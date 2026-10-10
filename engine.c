/*
 * MackEngine - motor de ajedrez UCI en C para MackBot
 * ----------------------------------------------------
 * - Bitboards + magic bitboards (se generan al arrancar, ~10 ms)
 * - Negamax / PVS, profundizacion iterativa, ventanas de aspiracion
 * - Tabla de transposicion (tamano configurable, Hash MB)
 * - Null-move, reverse futility, futility, LMP, LMR, IIR, extension por jaque
 * - Killers + history (con malus) + MVV-LVA
 * - Quiescence con delta pruning
 * - Evaluacion tapered (PST + peones pasados/aislados/doblados + movilidad +
 *   pareja de alfiles + torres en columnas abiertas + escudo del rey + mop-up)
 * - Gestion de tiempo propia (wtime/btime/winc/binc/movestogo/movetime)
 *
 * Compilar:  gcc -O3 -o mackengine engine.c -lm
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include <time.h>
#include <ctype.h>
#ifndef _WIN32
#include <sys/select.h>
#include <unistd.h>
#endif

typedef uint64_t U64;
#define C64(x) x##ULL
#define BIT(s) (C64(1) << (s))

enum { WHITE, BLACK };
enum { PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING };
#define EMPTY 12
#define PC(c, t) ((c) * 6 + (t))
#define TYPE(p) ((p) % 6)
#define COLOR(p) ((p) / 6)

#define popcnt(x) __builtin_popcountll(x)
#define lsb(x) __builtin_ctzll(x)
static inline int poplsb(U64 *b) { int s = lsb(*b); *b &= *b - 1; return s; }

/* ------------------------------------------------------------------ */
/* PRNG                                                                 */
/* ------------------------------------------------------------------ */
static U64 rng_state = C64(0x9E3779B97F4A7C15);
static U64 rnd64(void) {
    rng_state ^= rng_state >> 12;
    rng_state ^= rng_state << 25;
    rng_state ^= rng_state >> 27;
    return rng_state * C64(2685821657736338717);
}
static U64 rnd_sparse(void) { return rnd64() & rnd64() & rnd64(); }

/* ------------------------------------------------------------------ */
/* Tablas de ataque                                                     */
/* ------------------------------------------------------------------ */
static U64 knight_att[64], king_att[64], pawn_att[2][64];
static U64 rmask[64], bmask[64], rmagic[64], bmagic[64];
static int rshift[64], bshift[64];
static U64 *rptr[64], *bptr[64];
static U64 rtable[102400], btable[5248];

static const int RD[4] = {1, -1, 0, 0}, RF[4] = {0, 0, 1, -1};
static const int BD[4] = {1, 1, -1, -1}, BF[4] = {1, -1, 1, -1};

static U64 slide_att(int sq, U64 occ, const int *dr, const int *df) {
    U64 a = 0;
    int r0 = sq >> 3, f0 = sq & 7;
    for (int d = 0; d < 4; d++) {
        int r = r0 + dr[d], f = f0 + df[d];
        while (r >= 0 && r < 8 && f >= 0 && f < 8) {
            int s = r * 8 + f;
            a |= BIT(s);
            if (occ & BIT(s)) break;
            r += dr[d]; f += df[d];
        }
    }
    return a;
}

static U64 slide_mask(int sq, const int *dr, const int *df) {
    U64 a = 0;
    int r0 = sq >> 3, f0 = sq & 7;
    for (int d = 0; d < 4; d++) {
        int r = r0 + dr[d], f = f0 + df[d];
        while (r + dr[d] >= 0 && r + dr[d] < 8 && f + df[d] >= 0 && f + df[d] < 8) {
            a |= BIT(r * 8 + f);
            r += dr[d]; f += df[d];
        }
    }
    return a;
}

static void init_magics(int bishop) {
    U64 *mask = bishop ? bmask : rmask;
    U64 *magic = bishop ? bmagic : rmagic;
    int *shift = bishop ? bshift : rshift;
    U64 **ptr = bishop ? bptr : rptr;
    U64 *table = bishop ? btable : rtable;
    const int *dr = bishop ? BD : RD, *df = bishop ? BF : RF;
    size_t off = 0;
    static U64 occ[4096], ref[4096], att[4096];

    for (int sq = 0; sq < 64; sq++) {
        mask[sq] = slide_mask(sq, dr, df);
        int bits = popcnt(mask[sq]);
        int n = 1 << bits;
        shift[sq] = 64 - bits;
        U64 b = 0;
        int i = 0;
        do {
            occ[i] = b;
            ref[i] = slide_att(sq, b, dr, df);
            i++;
            b = (b - mask[sq]) & mask[sq];
        } while (b);
        ptr[sq] = table + off;
        for (;;) {
            U64 m = rnd_sparse();
            if (popcnt((mask[sq] * m) >> 56) < 6) continue;
            memset(att, 0, sizeof(U64) * n);
            int ok = 1;
            for (i = 0; i < n; i++) {
                int idx = (int)((occ[i] * m) >> shift[sq]);
                if (!att[idx]) att[idx] = ref[i];
                else if (att[idx] != ref[i]) { ok = 0; break; }
            }
            if (ok) { magic[sq] = m; break; }
        }
        memcpy(ptr[sq], att, sizeof(U64) * n);
        off += n;
    }
}

static inline U64 rook_att(int sq, U64 occ) {
    return rptr[sq][((occ & rmask[sq]) * rmagic[sq]) >> rshift[sq]];
}
static inline U64 bishop_att(int sq, U64 occ) {
    return bptr[sq][((occ & bmask[sq]) * bmagic[sq]) >> bshift[sq]];
}

static void init_attacks(void) {
    static const int kn[8][2] = {{1,2},{2,1},{2,-1},{1,-2},{-1,-2},{-2,-1},{-2,1},{-1,2}};
    static const int kg[8][2] = {{1,0},{1,1},{0,1},{-1,1},{-1,0},{-1,-1},{0,-1},{1,-1}};
    for (int sq = 0; sq < 64; sq++) {
        int r = sq >> 3, f = sq & 7;
        knight_att[sq] = king_att[sq] = pawn_att[0][sq] = pawn_att[1][sq] = 0;
        for (int i = 0; i < 8; i++) {
            int rr = r + kn[i][0], ff = f + kn[i][1];
            if (rr >= 0 && rr < 8 && ff >= 0 && ff < 8) knight_att[sq] |= BIT(rr * 8 + ff);
            rr = r + kg[i][0]; ff = f + kg[i][1];
            if (rr >= 0 && rr < 8 && ff >= 0 && ff < 8) king_att[sq] |= BIT(rr * 8 + ff);
        }
        for (int df = -1; df <= 1; df += 2) {
            if (f + df < 0 || f + df > 7) continue;
            if (r < 7) pawn_att[WHITE][sq] |= BIT((r + 1) * 8 + f + df);
            if (r > 0) pawn_att[BLACK][sq] |= BIT((r - 1) * 8 + f + df);
        }
    }
    init_magics(0);
    init_magics(1);
}

/* ------------------------------------------------------------------ */
/* Posicion                                                             */
/* ------------------------------------------------------------------ */
typedef struct {
    U64 bb[12];
    U64 occ[2];
    U64 all;
    int mb[64];
    int side, castle, ep, half;
    U64 key;
} Pos;

typedef struct { U64 key; int castle, ep, half, cap; } Undo;

#define MAXHIST 4096
static U64 hkey[MAXHIST];
static Undo undo[MAXHIST];
static int hn = 0;

static U64 zob_pc[12][64], zob_castle[16], zob_ep[8], zob_side;
static int cmask[64];

static void init_zobrist(void) {
    for (int i = 0; i < 12; i++) for (int s = 0; s < 64; s++) zob_pc[i][s] = rnd64();
    for (int i = 0; i < 16; i++) zob_castle[i] = rnd64();
    for (int i = 0; i < 8; i++) zob_ep[i] = rnd64();
    zob_side = rnd64();
    for (int s = 0; s < 64; s++) cmask[s] = 15;
    cmask[0] = 13; cmask[7] = 14; cmask[4] = 12;     /* blancas: 1=O-O 2=O-O-O */
    cmask[56] = 7; cmask[63] = 11; cmask[60] = 3;    /* negras:  4=O-O 8=O-O-O */
}

static inline void put(Pos *p, int pc, int sq) {
    p->bb[pc] |= BIT(sq); p->occ[pc / 6] |= BIT(sq); p->all |= BIT(sq);
    p->mb[sq] = pc; p->key ^= zob_pc[pc][sq];
}
static inline void rem(Pos *p, int pc, int sq) {
    p->bb[pc] &= ~BIT(sq); p->occ[pc / 6] &= ~BIT(sq); p->all &= ~BIT(sq);
    p->mb[sq] = EMPTY; p->key ^= zob_pc[pc][sq];
}

static U64 compute_key(const Pos *p) {
    U64 k = 0;
    for (int s = 0; s < 64; s++) if (p->mb[s] != EMPTY) k ^= zob_pc[p->mb[s]][s];
    k ^= zob_castle[p->castle];
    if (p->ep != -1) k ^= zob_ep[p->ep & 7];
    if (p->side == BLACK) k ^= zob_side;
    return k;
}

static const char *STARTFEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

static void set_fen(Pos *p, const char *fen) {
    memset(p, 0, sizeof(*p));
    for (int i = 0; i < 64; i++) p->mb[i] = EMPTY;
    p->ep = -1;
    const char *c = fen;
    int sq = 56;
    while (*c && *c != ' ') {
        if (*c == '/') sq -= 16;
        else if (isdigit((unsigned char)*c)) sq += *c - '0';
        else {
            int col = isupper((unsigned char)*c) ? WHITE : BLACK, t = PAWN;
            switch (tolower((unsigned char)*c)) {
                case 'p': t = PAWN; break; case 'n': t = KNIGHT; break;
                case 'b': t = BISHOP; break; case 'r': t = ROOK; break;
                case 'q': t = QUEEN; break; case 'k': t = KING; break;
            }
            if (sq >= 0 && sq < 64) put(p, PC(col, t), sq);
            sq++;
        }
        c++;
    }
    char side[4] = "w", cs[8] = "-", eps[4] = "-";
    int half = 0, full = 1;
    sscanf(c, " %3s %7s %3s %d %d", side, cs, eps, &half, &full);
    p->side = (side[0] == 'b') ? BLACK : WHITE;
    p->castle = 0;
    for (const char *q = cs; *q; q++) {
        if (*q == 'K') p->castle |= 1;
        else if (*q == 'Q') p->castle |= 2;
        else if (*q == 'k') p->castle |= 4;
        else if (*q == 'q') p->castle |= 8;
    }
    if (eps[0] >= 'a' && eps[0] <= 'h' && eps[1] >= '1' && eps[1] <= '8')
        p->ep = (eps[0] - 'a') + 8 * (eps[1] - '1');
    p->half = half;
    p->key = compute_key(p);
    hn = 0;
}

/* ------------------------------------------------------------------ */
/* Ataques / jaque                                                      */
/* ------------------------------------------------------------------ */
static inline int attacked(const Pos *p, int sq, int by) {
    if (pawn_att[by ^ 1][sq] & p->bb[PC(by, PAWN)]) return 1;
    if (knight_att[sq] & p->bb[PC(by, KNIGHT)]) return 1;
    if (king_att[sq] & p->bb[PC(by, KING)]) return 1;
    if (bishop_att(sq, p->all) & (p->bb[PC(by, BISHOP)] | p->bb[PC(by, QUEEN)])) return 1;
    if (rook_att(sq, p->all) & (p->bb[PC(by, ROOK)] | p->bb[PC(by, QUEEN)])) return 1;
    return 0;
}
static inline int in_check(const Pos *p) {
    U64 k = p->bb[PC(p->side, KING)];
    if (!k) return 0;
    return attacked(p, lsb(k), p->side ^ 1);
}

/* ------------------------------------------------------------------ */
/* Jugadas                                                              */
/* ------------------------------------------------------------------ */
typedef int Move;
#define MFROM(m) ((m) & 63)
#define MTO(m) (((m) >> 6) & 63)
#define MPROMO(m) (((m) >> 12) & 7)
#define MFLAG(m) (((m) >> 16) & 7)
#define F_EP 1
#define F_CASTLE 2
#define F_DOUBLE 4
#define MK(f, t, pr, fl) ((f) | ((t) << 6) | ((pr) << 12) | ((fl) << 16))

typedef struct { Move m[256]; int sc[256]; int n; } MList;

static void make_move(Pos *p, Move m) {
    int from = MFROM(m), to = MTO(m), promo = MPROMO(m), fl = MFLAG(m);
    int us = p->side, them = us ^ 1, pc = p->mb[from];
    Undo *u = &undo[hn];
    u->key = p->key; u->castle = p->castle; u->ep = p->ep; u->half = p->half; u->cap = p->mb[to];
    hkey[hn] = p->key;
    hn++;
    if (p->ep != -1) { p->key ^= zob_ep[p->ep & 7]; p->ep = -1; }
    p->half++;
    if (TYPE(pc) == PAWN) p->half = 0;
    if (fl & F_EP) {
        rem(p, PC(them, PAWN), to + (us == WHITE ? -8 : 8));
        p->half = 0;
    } else if (p->mb[to] != EMPTY) {
        rem(p, p->mb[to], to);
        p->half = 0;
    }
    rem(p, pc, from);
    put(p, promo ? PC(us, promo) : pc, to);
    if (fl & F_CASTLE) {
        int rf, rt;
        switch (to) {
            case 6:  rf = 7;  rt = 5;  break;
            case 2:  rf = 0;  rt = 3;  break;
            case 62: rf = 63; rt = 61; break;
            default: rf = 56; rt = 59; break;
        }
        int rp = PC(us, ROOK);
        rem(p, rp, rf); put(p, rp, rt);
    }
    if (fl & F_DOUBLE) { p->ep = (from + to) / 2; p->key ^= zob_ep[p->ep & 7]; }
    p->key ^= zob_castle[p->castle];
    p->castle &= cmask[from] & cmask[to];
    p->key ^= zob_castle[p->castle];
    p->side = them;
    p->key ^= zob_side;
}

static void unmake_move(Pos *p, Move m) {
    hn--;
    Undo *u = &undo[hn];
    int from = MFROM(m), to = MTO(m), promo = MPROMO(m), fl = MFLAG(m);
    p->side ^= 1;
    int us = p->side, them = us ^ 1;
    int pc = p->mb[to];
    rem(p, pc, to);
    put(p, promo ? PC(us, PAWN) : pc, from);
    if (fl & F_EP) put(p, PC(them, PAWN), to + (us == WHITE ? -8 : 8));
    else if (u->cap != EMPTY) put(p, u->cap, to);
    if (fl & F_CASTLE) {
        int rf, rt;
        switch (to) {
            case 6:  rf = 7;  rt = 5;  break;
            case 2:  rf = 0;  rt = 3;  break;
            case 62: rf = 63; rt = 61; break;
            default: rf = 56; rt = 59; break;
        }
        int rp = PC(us, ROOK);
        rem(p, rp, rt); put(p, rp, rf);
    }
    p->castle = u->castle; p->ep = u->ep; p->half = u->half; p->key = u->key;
}

static void make_null(Pos *p) {
    Undo *u = &undo[hn];
    u->key = p->key; u->castle = p->castle; u->ep = p->ep; u->half = p->half; u->cap = EMPTY;
    hkey[hn] = p->key;
    hn++;
    if (p->ep != -1) { p->key ^= zob_ep[p->ep & 7]; p->ep = -1; }
    p->side ^= 1;
    p->key ^= zob_side;
    p->half++;
}
static void unmake_null(Pos *p) {
    hn--;
    Undo *u = &undo[hn];
    p->side ^= 1;
    p->ep = u->ep; p->half = u->half; p->key = u->key;
}

static inline int make_legal(Pos *p, Move m) {
    int us = p->side;
    make_move(p, m);
    if (attacked(p, lsb(p->bb[PC(us, KING)]), us ^ 1)) { unmake_move(p, m); return 0; }
    return 1;
}

static inline void add_move(MList *L, Move m) { L->m[L->n++] = m; }

static void gen(const Pos *p, MList *L, int caps_only) {
    L->n = 0;
    int us = p->side, them = us ^ 1;
    U64 own = p->occ[us], opp = p->occ[them], all = p->all;
    U64 targets = caps_only ? opp : ~own;

    U64 pawns = p->bb[PC(us, PAWN)];
    int up = (us == WHITE) ? 8 : -8;
    int promo_rank = (us == WHITE) ? 6 : 1, start_rank = (us == WHITE) ? 1 : 6;
    while (pawns) {
        int from = poplsb(&pawns), r = from >> 3, to = from + up;
        if (!(all & BIT(to))) {
            if (r == promo_rank) {
                add_move(L, MK(from, to, QUEEN, 0));
                if (!caps_only) {
                    add_move(L, MK(from, to, ROOK, 0));
                    add_move(L, MK(from, to, BISHOP, 0));
                    add_move(L, MK(from, to, KNIGHT, 0));
                }
            } else if (!caps_only) {
                add_move(L, MK(from, to, 0, 0));
                if (r == start_rank && !(all & BIT(to + up)))
                    add_move(L, MK(from, to + up, 0, F_DOUBLE));
            }
        }
        U64 att = pawn_att[us][from] & opp;
        while (att) {
            int t = poplsb(&att);
            if (r == promo_rank) {
                add_move(L, MK(from, t, QUEEN, 0));
                if (!caps_only) {
                    add_move(L, MK(from, t, ROOK, 0));
                    add_move(L, MK(from, t, BISHOP, 0));
                    add_move(L, MK(from, t, KNIGHT, 0));
                }
            } else add_move(L, MK(from, t, 0, 0));
        }
        if (p->ep != -1 && (pawn_att[us][from] & BIT(p->ep)))
            add_move(L, MK(from, p->ep, 0, F_EP));
    }
    for (int pt = KNIGHT; pt <= KING; pt++) {
        U64 b = p->bb[PC(us, pt)];
        while (b) {
            int from = poplsb(&b);
            U64 a;
            switch (pt) {
                case KNIGHT: a = knight_att[from]; break;
                case BISHOP: a = bishop_att(from, all); break;
                case ROOK:   a = rook_att(from, all); break;
                case QUEEN:  a = bishop_att(from, all) | rook_att(from, all); break;
                default:     a = king_att[from]; break;
            }
            a &= targets;
            while (a) add_move(L, MK(from, poplsb(&a), 0, 0));
        }
    }
    if (!caps_only) {
        if (us == WHITE) {
            if (p->mb[4] == PC(WHITE, KING)) {
                if ((p->castle & 1) && !(all & (BIT(5) | BIT(6))) &&
                    !attacked(p, 4, BLACK) && !attacked(p, 5, BLACK) && !attacked(p, 6, BLACK))
                    add_move(L, MK(4, 6, 0, F_CASTLE));
                if ((p->castle & 2) && !(all & (BIT(1) | BIT(2) | BIT(3))) &&
                    !attacked(p, 4, BLACK) && !attacked(p, 3, BLACK) && !attacked(p, 2, BLACK))
                    add_move(L, MK(4, 2, 0, F_CASTLE));
            }
        } else {
            if (p->mb[60] == PC(BLACK, KING)) {
                if ((p->castle & 4) && !(all & (BIT(61) | BIT(62))) &&
                    !attacked(p, 60, WHITE) && !attacked(p, 61, WHITE) && !attacked(p, 62, WHITE))
                    add_move(L, MK(60, 62, 0, F_CASTLE));
                if ((p->castle & 8) && !(all & (BIT(57) | BIT(58) | BIT(59))) &&
                    !attacked(p, 60, WHITE) && !attacked(p, 59, WHITE) && !attacked(p, 58, WHITE))
                    add_move(L, MK(60, 58, 0, F_CASTLE));
            }
        }
    }
}

static void move_str(Move m, char *out) {
    static const char pc[] = "?nbrq";
    int f = MFROM(m), t = MTO(m), pr = MPROMO(m);
    out[0] = 'a' + (f & 7); out[1] = '1' + (f >> 3);
    out[2] = 'a' + (t & 7); out[3] = '1' + (t >> 3);
    if (pr) { out[4] = pc[pr]; out[5] = 0; } else out[4] = 0;
}

static Move parse_move(Pos *p, const char *s) {
    if (strlen(s) < 4) return 0;
    int f = (s[0] - 'a') + 8 * (s[1] - '1'), t = (s[2] - 'a') + 8 * (s[3] - '1'), pr = 0;
    if (s[4]) {
        switch (s[4]) { case 'n': pr = KNIGHT; break; case 'b': pr = BISHOP; break;
                        case 'r': pr = ROOK; break; case 'q': pr = QUEEN; break; }
    }
    MList L;
    gen(p, &L, 0);
    for (int i = 0; i < L.n; i++) {
        Move m = L.m[i];
        if (MFROM(m) == f && MTO(m) == t && MPROMO(m) == pr) {
            if (make_legal(p, m)) { unmake_move(p, m); return m; }
        }
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* Evaluacion                                                           */
/* ------------------------------------------------------------------ */
static const int PST_P[64] = {0,0,0,0,0,0,0,0, 50,50,50,50,50,50,50,50, 10,10,20,30,30,20,10,10,
    5,5,10,25,25,10,5,5, 0,0,0,20,20,0,0,0, 5,-5,-10,0,0,-10,-5,5, 5,10,10,-20,-20,10,10,5, 0,0,0,0,0,0,0,0};
static const int PST_N[64] = {-50,-40,-30,-30,-30,-30,-40,-50, -40,-20,0,0,0,0,-20,-40, -30,0,10,15,15,10,0,-30,
    -30,5,15,20,20,15,5,-30, -30,0,15,20,20,15,0,-30, -30,5,10,15,15,10,5,-30, -40,-20,0,5,5,0,-20,-40, -50,-40,-30,-30,-30,-30,-40,-50};
static const int PST_B[64] = {-20,-10,-10,-10,-10,-10,-10,-20, -10,0,0,0,0,0,0,-10, -10,0,5,10,10,5,0,-10,
    -10,5,5,10,10,5,5,-10, -10,0,10,10,10,10,0,-10, -10,10,10,10,10,10,10,-10, -10,5,0,0,0,0,5,-10, -20,-10,-10,-10,-10,-10,-10,-20};
static const int PST_R[64] = {0,0,0,0,0,0,0,0, 5,10,10,10,10,10,10,5, -5,0,0,0,0,0,0,-5, -5,0,0,0,0,0,0,-5,
    -5,0,0,0,0,0,0,-5, -5,0,0,0,0,0,0,-5, -5,0,0,0,0,0,0,-5, 0,0,0,5,5,0,0,0};
static const int PST_Q[64] = {-20,-10,-10,-5,-5,-10,-10,-20, -10,0,0,0,0,0,0,-10, -10,0,5,5,5,5,0,-10, -5,0,5,5,5,5,0,-5,
    0,0,5,5,5,5,0,-5, -10,5,5,5,5,5,0,-10, -10,0,5,0,0,0,0,-10, -20,-10,-10,-5,-5,-10,-10,-20};
static const int PST_K_MG[64] = {-30,-40,-40,-50,-50,-40,-40,-30, -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30, -30,-40,-40,-50,-50,-40,-40,-30, -20,-30,-30,-40,-40,-30,-30,-20,
    -10,-20,-20,-20,-20,-20,-20,-10, 20,20,0,0,0,0,20,20, 20,30,10,0,0,10,30,20};
static const int PST_K_EG[64] = {-50,-40,-30,-20,-20,-30,-40,-50, -30,-20,-10,0,0,-10,-20,-30, -30,-10,20,30,30,20,-10,-30,
    -30,-10,30,40,40,30,-10,-30, -30,-10,30,40,40,30,-10,-30, -30,-10,20,30,30,20,-10,-30, -30,-30,0,0,0,0,-30,-30, -50,-30,-30,-30,-30,-30,-30,-50};
static const int PAWN_EG_ROW[8] = {0, 90, 60, 40, 25, 15, 10, 0};

static const int MAT_MG[6] = {100, 320, 330, 500, 900, 0};
static const int MAT_EG[6] = {115, 310, 320, 520, 900, 0};
static const int PHW[6] = {0, 1, 1, 2, 4, 0};
static const int PASSED_MG[8] = {0, 5, 10, 20, 40, 70, 110, 0};
static const int PASSED_EG[8] = {0, 10, 20, 40, 75, 120, 180, 0};

static int mg_tab[12][64], eg_tab[12][64];
static U64 file_bb[8], adj_bb[8], passed_mask[2][64], shield_mask[2][64];
static int cmd_tab[64];

static void init_eval(void) {
    for (int f = 0; f < 8; f++) file_bb[f] = C64(0x0101010101010101) << f;
    for (int f = 0; f < 8; f++) adj_bb[f] = (f > 0 ? file_bb[f - 1] : 0) | (f < 7 ? file_bb[f + 1] : 0);
    for (int c = 0; c < 2; c++) {
        for (int pt = 0; pt < 6; pt++) {
            for (int s = 0; s < 64; s++) {
                int idx = (c == WHITE) ? (s ^ 56) : s;
                int pm = 0, pe = 0;
                switch (pt) {
                    case PAWN:   pm = PST_P[idx]; pe = PAWN_EG_ROW[idx >> 3]; break;
                    case KNIGHT: pm = pe = PST_N[idx]; break;
                    case BISHOP: pm = pe = PST_B[idx]; break;
                    case ROOK:   pm = pe = PST_R[idx]; break;
                    case QUEEN:  pm = pe = PST_Q[idx]; break;
                    case KING:   pm = PST_K_MG[idx]; pe = PST_K_EG[idx]; break;
                }
                mg_tab[PC(c, pt)][s] = MAT_MG[pt] + pm;
                eg_tab[PC(c, pt)][s] = MAT_EG[pt] + pe;
            }
        }
    }
    for (int sq = 0; sq < 64; sq++) {
        int f = sq & 7, r = sq >> 3;
        for (int c = 0; c < 2; c++) {
            U64 m = 0, sh = 0;
            if (c == WHITE) { for (int rr = r + 1; rr < 8; rr++) for (int ff = f - 1; ff <= f + 1; ff++) if (ff >= 0 && ff < 8) m |= BIT(rr * 8 + ff); }
            else            { for (int rr = 0; rr < r; rr++)     for (int ff = f - 1; ff <= f + 1; ff++) if (ff >= 0 && ff < 8) m |= BIT(rr * 8 + ff); }
            for (int d = 1; d <= 2; d++) {
                int rr = r + (c == WHITE ? d : -d);
                if (rr < 0 || rr > 7) continue;
                for (int ff = f - 1; ff <= f + 1; ff++) if (ff >= 0 && ff < 8) sh |= BIT(rr * 8 + ff);
            }
            passed_mask[c][sq] = m;
            shield_mask[c][sq] = sh;
        }
        int dr = (3 - r > r - 4) ? 3 - r : r - 4, df = (3 - f > f - 4) ? 3 - f : f - 4;
        cmd_tab[sq] = dr + df;
    }
}

static int evaluate(const Pos *p) {
    int mg = 0, eg = 0, phase = 0;
    int npm[2] = {0, 0};
    for (int c = 0; c < 2; c++) {
        int sign = (c == WHITE) ? 1 : -1, m = 0, e = 0;
        U64 own_p = p->bb[PC(c, PAWN)], en_p = p->bb[PC(c ^ 1, PAWN)];
        U64 not_own = ~p->occ[c];
        for (int pt = 0; pt < 6; pt++) {
            U64 b = p->bb[PC(c, pt)];
            int cnt = popcnt(b);
            phase += PHW[pt] * cnt;
            if (pt >= KNIGHT && pt <= QUEEN) npm[c] += cnt * MAT_MG[pt];
            const int *tm = mg_tab[PC(c, pt)], *te = eg_tab[PC(c, pt)];
            while (b) {
                int sq = poplsb(&b);
                m += tm[sq]; e += te[sq];
                switch (pt) {
                    case PAWN: {
                        int f = sq & 7;
                        if (!(passed_mask[c][sq] & en_p)) {
                            int rk = (c == WHITE) ? (sq >> 3) : 7 - (sq >> 3);
                            m += PASSED_MG[rk]; e += PASSED_EG[rk];
                        }
                        if (!(adj_bb[f] & own_p)) { m -= 10; e -= 15; }
                        break;
                    }
                    case KNIGHT: {
                        int mob = popcnt(knight_att[sq] & not_own) - 4;
                        m += mob * 4; e += mob * 4;
                        break;
                    }
                    case BISHOP: {
                        int mob = popcnt(bishop_att(sq, p->all) & not_own) - 6;
                        m += mob * 3; e += mob * 4;
                        break;
                    }
                    case ROOK: {
                        U64 fm = file_bb[sq & 7];
                        if (!(fm & own_p)) {
                            m += 12; e += 8;
                            if (!(fm & en_p)) { m += 10; e += 6; }
                        }
                        int mob = popcnt(rook_att(sq, p->all) & not_own) - 7;
                        m += mob * 2; e += mob * 4;
                        break;
                    }
                    case QUEEN: {
                        int mob = popcnt((bishop_att(sq, p->all) | rook_att(sq, p->all)) & not_own) - 14;
                        m += mob; e += mob * 2;
                        break;
                    }
                    case KING:
                        m += 9 * popcnt(shield_mask[c][sq] & own_p);
                        break;
                }
            }
        }
        if (popcnt(p->bb[PC(c, BISHOP)]) >= 2) { m += 30; e += 45; }
        for (int f = 0; f < 8; f++) {
            int n = popcnt(own_p & file_bb[f]);
            if (n > 1) { m -= 12 * (n - 1); e -= 18 * (n - 1); }
        }
        mg += sign * m; eg += sign * e;
    }
    /* mop-up: rey solo contra material suficiente */
    for (int c = 0; c < 2; c++) {
        int l = c ^ 1;
        if (npm[l] == 0 && !p->bb[PC(l, PAWN)] && npm[c] >= 500) {
            int ks = lsb(p->bb[PC(c, KING)]), ls = lsb(p->bb[PC(l, KING)]);
            int dr = abs((ks >> 3) - (ls >> 3)), df = abs((ks & 7) - (ls & 7));
            int bonus = 10 * cmd_tab[ls] + 4 * (14 - (dr + df));
            eg += (c == WHITE ? 1 : -1) * bonus;
        }
    }
    if (phase > 24) phase = 24;
    int score = (mg * phase + eg * (24 - phase)) / 24;
    score += (p->side == WHITE) ? 10 : -10;
    return (p->side == WHITE) ? score : -score;
}

/* ------------------------------------------------------------------ */
/* Busqueda                                                             */
/* ------------------------------------------------------------------ */
#define MAXPLY 128
#define INF 32000
#define MATE 30000
#define MATE_BOUND (MATE - MAXPLY)
#define EXACT 0
#define LOWER 1
#define UPPER 2

typedef struct { U64 key; Move move; int16_t score; int8_t depth; uint8_t fa; } TTEntry;

static TTEntry *tt = NULL;
static U64 tt_mask = 0;
static int tt_age = 0;
static int hash_mb = 64;

static void tt_alloc(int mb) {
    if (mb < 1) mb = 1;
    U64 want = ((U64)mb * 1024 * 1024) / sizeof(TTEntry), n = 1;
    while (n * 2 <= want) n *= 2;
    free(tt);
    tt = (TTEntry *)calloc(n, sizeof(TTEntry));
    if (!tt) { n = 1 << 16; tt = (TTEntry *)calloc(n, sizeof(TTEntry)); }
    tt_mask = n - 1;
    hash_mb = mb;
}

static Move killers[MAXPLY + 4][2];
static int history[2][64][64];
static int sev[MAXPLY + 4];
static int lmr_tab[64][64];
static const int VORD[6] = {1, 3, 3, 5, 9, 0};
static const int VAL[6] = {100, 320, 330, 500, 900, 20000};

static long long nodes = 0, node_limit = 0;
static int stop_flag = 0, infinite_mode = 0;
static double t_start = 0, hard_deadline = 1e18;
static Move root_best = 0;
static int move_overhead_ms = 100;

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static void check_stop(void) {
    if (!infinite_mode && now_s() >= hard_deadline) stop_flag = 1;
    if (node_limit && nodes >= node_limit) stop_flag = 1;
#ifndef _WIN32
    fd_set rf;
    struct timeval tv = {0, 0};
    FD_ZERO(&rf);
    FD_SET(0, &rf);
    if (select(1, &rf, NULL, NULL, &tv) > 0) {
        char buf[512];
        if (fgets(buf, sizeof buf, stdin)) {
            if (!strncmp(buf, "stop", 4)) stop_flag = 1;
            else if (!strncmp(buf, "quit", 4)) exit(0);
            else if (!strncmp(buf, "isready", 7)) { puts("readyok"); fflush(stdout); }
        } else exit(0);
    }
#endif
}

static inline int is_capture(const Pos *p, Move m) {
    return p->mb[MTO(m)] != EMPTY || (MFLAG(m) & F_EP);
}

static int is_repetition(const Pos *p) {
    int lim = p->half < hn ? p->half : hn;
    for (int i = 2; i <= lim; i += 2) if (hkey[hn - i] == p->key) return 1;
    return 0;
}

static int insufficient(const Pos *p) {
    U64 heavy = p->bb[0] | p->bb[6] | p->bb[3] | p->bb[9] | p->bb[4] | p->bb[10];
    if (heavy) return 0;
    U64 minors = p->bb[1] | p->bb[2] | p->bb[7] | p->bb[8];
    return popcnt(minors) <= 1;
}

static void score_moves(const Pos *p, MList *L, Move ttm, int ply) {
    int them = p->side ^ 1;
    for (int i = 0; i < L->n; i++) {
        Move m = L->m[i];
        int sc;
        if (m == ttm) sc = 1 << 30;
        else {
            int to = MTO(m), victim = -1;
            if (MFLAG(m) & F_EP) victim = PAWN;
            else if (p->mb[to] != EMPTY) victim = TYPE(p->mb[to]);
            if (victim >= 0) {
                int att = TYPE(p->mb[MFROM(m)]);
                sc = VORD[victim] * 16 - VORD[att];
                if (VAL[att] > VAL[victim] + 50 && attacked(p, to, them)) sc -= 200000;
                else sc += 1000000;
            } else if (MPROMO(m)) sc = 900000 + MPROMO(m);
            else if (m == killers[ply][0]) sc = 800000;
            else if (m == killers[ply][1]) sc = 700000;
            else sc = history[p->side][MFROM(m)][MTO(m)];
        }
        L->sc[i] = sc;
    }
}

static inline Move pick(MList *L, int i) {
    int best = i;
    for (int j = i + 1; j < L->n; j++) if (L->sc[j] > L->sc[best]) best = j;
    if (best != i) {
        Move tm = L->m[i]; L->m[i] = L->m[best]; L->m[best] = tm;
        int ts = L->sc[i]; L->sc[i] = L->sc[best]; L->sc[best] = ts;
    }
    return L->m[i];
}

static inline void hist_update(int side, Move m, int bonus) {
    int *h = &history[side][MFROM(m)][MTO(m)];
    *h += bonus - (*h) * abs(bonus) / 16384;
}

static int qsearch(Pos *p, int alpha, int beta, int ply) {
    if ((++nodes & 2047) == 0) check_stop();
    if (stop_flag) return 0;
    if (ply >= MAXPLY - 1) return evaluate(p);

    int chk = in_check(p), best, stand = -INF;
    MList L;
    if (chk) {
        best = -MATE + ply;
        gen(p, &L, 0);
    } else {
        stand = evaluate(p);
        if (stand >= beta) return stand;
        if (stand > alpha) alpha = stand;
        best = stand;
        gen(p, &L, 1);
    }
    score_moves(p, &L, 0, ply);
    int them = p->side ^ 1;
    for (int i = 0; i < L.n; i++) {
        Move m = pick(&L, i);
        if (!chk && !MPROMO(m)) {
            int victim = (MFLAG(m) & F_EP) ? PAWN : TYPE(p->mb[MTO(m)]);
            int att = TYPE(p->mb[MFROM(m)]);
            if (stand + VAL[victim] + 200 < alpha) continue;
            if (VAL[att] > VAL[victim] + 50 && attacked(p, MTO(m), them)) continue;
        }
        if (!make_legal(p, m)) continue;
        int score = -qsearch(p, -beta, -alpha, ply + 1);
        unmake_move(p, m);
        if (stop_flag) return 0;
        if (score > best) {
            best = score;
            if (score > alpha) {
                alpha = score;
                if (alpha >= beta) break;
            }
        }
    }
    return best;
}

static int search(Pos *p, int depth, int alpha, int beta, int ply, int allow_null) {
    int pv = (beta - alpha) > 1;
    if ((++nodes & 2047) == 0) check_stop();
    if (stop_flag) return 0;

    if (ply > 0) {
        if (p->half >= 100 || is_repetition(p) || insufficient(p)) return 0;
        if (alpha < -MATE + ply) alpha = -MATE + ply;
        if (beta > MATE - ply - 1) beta = MATE - ply - 1;
        if (alpha >= beta) return alpha;
    }
    if (ply >= MAXPLY - 1) return evaluate(p);

    int chk = in_check(p);
    if (chk) depth++;
    if (depth <= 0) return qsearch(p, alpha, beta, ply);

    /* --- TT --- */
    TTEntry *e = &tt[p->key & tt_mask];
    Move ttm = 0;
    if (e->key == p->key) {
        ttm = e->move;
        int tts = e->score, flag = e->fa & 3;
        if (tts > MATE_BOUND) tts -= ply; else if (tts < -MATE_BOUND) tts += ply;
        if (!pv && ply > 0 && e->depth >= depth) {
            if (flag == EXACT) return tts;
            if (flag == LOWER && tts >= beta) return tts;
            if (flag == UPPER && tts <= alpha) return tts;
        }
    }

    int stat = -INF, improving = 0;
    if (chk) sev[ply] = -INF;
    else {
        stat = evaluate(p);
        sev[ply] = stat;
        improving = (ply >= 2 && sev[ply - 2] != -INF && stat > sev[ply - 2]);
    }

    int us = p->side;
    if (!pv && !chk) {
        if (depth <= 6 && stat - 85 * depth + (improving ? 40 : 0) >= beta && abs(beta) < MATE_BOUND)
            return stat;
        if (allow_null && depth >= 3 && stat >= beta &&
            (p->occ[us] & ~(p->bb[PC(us, PAWN)] | p->bb[PC(us, KING)]))) {
            int R = 3 + depth / 4 + ((stat - beta) / 200 > 3 ? 3 : (stat - beta) / 200);
            make_null(p);
            int s = -search(p, depth - 1 - R, -beta, -beta + 1, ply + 1, 0);
            unmake_null(p);
            if (stop_flag) return 0;
            if (s >= beta) return s >= MATE_BOUND ? beta : s;
        }
    }
    if (depth >= 4 && !ttm) depth--;   /* IIR */

    MList L;
    gen(p, &L, 0);
    score_moves(p, &L, ttm, ply);

    int best = -INF, legal = 0, orig_alpha = alpha;
    Move best_move = 0, quiets[64];
    int nq = 0;

    for (int i = 0; i < L.n; i++) {
        Move m = pick(&L, i);
        int quiet = !is_capture(p, m) && !MPROMO(m);
        if (!make_legal(p, m)) continue;
        legal++;
        int gives = in_check(p);

        if (quiet && !pv && !chk && !gives && legal > 1 && best > -MATE_BOUND) {
            if (depth <= 3 && legal > 4 + depth * depth) { unmake_move(p, m); continue; }
            if (depth <= 2 && stat + 130 * depth <= alpha) { unmake_move(p, m); continue; }
        }

        int newd = depth - 1, score;
        if (legal == 1) {
            score = -search(p, newd, -beta, -alpha, ply + 1, 1);
        } else {
            int r = 0;
            if (depth >= 3 && legal >= 3 && quiet && !chk && !gives) {
                r = lmr_tab[depth > 63 ? 63 : depth][legal > 63 ? 63 : legal];
                if (pv) r--;
                if (improving) r--;
                if (m == killers[ply][0] || m == killers[ply][1]) r--;
                if (r > newd - 1) r = newd - 1;
                if (r < 0) r = 0;
            }
            score = -search(p, newd - r, -alpha - 1, -alpha, ply + 1, 1);
            if (score > alpha && r > 0) score = -search(p, newd, -alpha - 1, -alpha, ply + 1, 1);
            if (score > alpha && score < beta) score = -search(p, newd, -beta, -alpha, ply + 1, 1);
        }
        unmake_move(p, m);
        if (stop_flag) return 0;

        if (score > best) {
            best = score;
            best_move = m;
            if (score > alpha) {
                alpha = score;
                if (ply == 0) root_best = m;
                if (alpha >= beta) {
                    if (quiet) {
                        if (killers[ply][0] != m) { killers[ply][1] = killers[ply][0]; killers[ply][0] = m; }
                        int bonus = depth * depth;
                        if (bonus > 1600) bonus = 1600;
                        hist_update(us, m, bonus);
                        for (int q = 0; q < nq; q++) hist_update(us, quiets[q], -bonus);
                    }
                    break;
                }
            }
        }
        if (quiet && nq < 64) quiets[nq++] = m;
    }

    if (!legal) return chk ? -MATE + ply : 0;

    int flag = (best <= orig_alpha) ? UPPER : (best >= beta) ? LOWER : EXACT;
    int stored = best;
    if (stored > MATE_BOUND) stored += ply; else if (stored < -MATE_BOUND) stored -= ply;
    if (e->key != p->key || (e->fa >> 2) != tt_age || depth >= e->depth - 2 || flag == EXACT) {
        e->key = p->key;
        e->move = best_move ? best_move : (e->key == p->key ? e->move : 0);
        e->score = (int16_t)stored;
        e->depth = (int8_t)(depth > 127 ? 127 : depth);
        e->fa = (uint8_t)(flag | (tt_age << 2));
    }
    return best;
}

static void print_info(int depth, int score, double elapsed) {
    char mv[8];
    move_str(root_best, mv);
    long long nps = elapsed > 0.001 ? (long long)(nodes / elapsed) : nodes;
    if (abs(score) > MATE_BOUND) {
        int plies = MATE - abs(score);
        int mate_in = (plies + 1) / 2;
        printf("info depth %d score mate %d nodes %lld nps %lld time %d pv %s\n",
               depth, score > 0 ? mate_in : -mate_in, nodes, nps, (int)(elapsed * 1000), mv);
    } else {
        printf("info depth %d score cp %d nodes %lld nps %lld time %d pv %s\n",
               depth, score, nodes, nps, (int)(elapsed * 1000), mv);
    }
    fflush(stdout);
}

static void think(Pos *p, double soft, double hard, int maxd, long long maxnodes, int infinite) {
    t_start = now_s();
    hard_deadline = infinite ? 1e18 : t_start + hard;
    infinite_mode = infinite;
    node_limit = maxnodes;
    stop_flag = 0;
    nodes = 0;
    tt_age = (tt_age + 1) & 63;
    for (int s = 0; s < 2; s++) for (int a = 0; a < 64; a++) for (int b = 0; b < 64; b++) history[s][a][b] /= 2;
    memset(killers, 0, sizeof killers);

    MList L;
    gen(p, &L, 0);
    Move first = 0;
    int nlegal = 0;
    for (int i = 0; i < L.n; i++) {
        if (make_legal(p, L.m[i])) { unmake_move(p, L.m[i]); if (!first) first = L.m[i]; nlegal++; }
    }
    char mv[8];
    if (nlegal == 0) { printf("bestmove 0000\n"); fflush(stdout); return; }
    if (nlegal == 1 && !infinite) { move_str(first, mv); printf("bestmove %s\n", mv); fflush(stdout); return; }

    root_best = first;
    int score = 0;
    if (maxd < 1 || maxd > MAXPLY - 4) maxd = MAXPLY - 4;
    for (int depth = 1; depth <= maxd; depth++) {
        int alpha = -INF, beta = INF, delta = 35;
        if (depth >= 5) { alpha = score - delta; beta = score + delta; }
        for (;;) {
            int s = search(p, depth, alpha, beta, 0, 0);
            if (stop_flag) break;
            if (s <= alpha) alpha = (alpha < -1000) ? -INF : alpha - delta * 3;
            else if (s >= beta) beta = (beta > 1000) ? INF : beta + delta * 3;
            else { score = s; break; }
            delta *= 2;
        }
        if (stop_flag) break;
        double el = now_s() - t_start;
        print_info(depth, score, el);
        if (!infinite) {
            if (el > soft * 0.55) break;
            if (abs(score) > MATE_BOUND && depth >= 4) break;
        }
    }
    /* en modo infinite esperamos al 'stop' si la busqueda termino antes */
    move_str(root_best, mv);
    printf("bestmove %s\n", mv);
    fflush(stdout);
}

/* ------------------------------------------------------------------ */
/* Perft (para depurar)                                                 */
/* ------------------------------------------------------------------ */
static U64 perft(Pos *p, int depth) {
    if (depth == 0) return 1;
    MList L;
    gen(p, &L, 0);
    U64 n = 0;
    for (int i = 0; i < L.n; i++) {
        if (!make_legal(p, L.m[i])) continue;
        n += perft(p, depth - 1);
        unmake_move(p, L.m[i]);
    }
    return n;
}

/* ------------------------------------------------------------------ */
/* UCI                                                                  */
/* ------------------------------------------------------------------ */
static void init_all(void) {
    init_attacks();
    init_zobrist();
    init_eval();
    for (int d = 1; d < 64; d++)
        for (int i = 1; i < 64; i++)
            lmr_tab[d][i] = (int)(0.75 + log((double)d) * log((double)i) / 2.25);
    tt_alloc(hash_mb);
}

static void new_game(void) {
    memset(tt, 0, (tt_mask + 1) * sizeof(TTEntry));
    memset(history, 0, sizeof history);
    memset(killers, 0, sizeof killers);
    tt_age = 0;
}

static void cmd_position(Pos *p, char *args) {
    char *mv = strstr(args, " moves");
    if (mv) { *mv = 0; mv += 6; }
    while (*args == ' ') args++;
    if (!strncmp(args, "startpos", 8)) set_fen(p, STARTFEN);
    else if (!strncmp(args, "fen", 3)) { args += 3; while (*args == ' ') args++; set_fen(p, args); }
    else set_fen(p, STARTFEN);
    if (mv) {
        char *sp = NULL;
        for (char *t = strtok_r(mv, " \t\r\n", &sp); t; t = strtok_r(NULL, " \t\r\n", &sp)) {
            Move m = parse_move(p, t);
            if (!m) break;
            make_move(p, m);
        }
    }
}

static void cmd_go(Pos *p, char *args) {
    long long wtime = -1, btime = -1, winc = 0, binc = 0, movetime = -1, nodes_lim = 0;
    int mtg = 0, depth = 0, infinite = 0;
    char *sp = NULL;
    for (char *t = strtok_r(args, " \t\r\n", &sp); t; t = strtok_r(NULL, " \t\r\n", &sp)) {
        char *v;
        if (!strcmp(t, "infinite")) infinite = 1;
        else if (!strcmp(t, "ponder")) { /* ignorado */ }
        else if (!strcmp(t, "wtime") && (v = strtok_r(NULL, " ", &sp))) wtime = atoll(v);
        else if (!strcmp(t, "btime") && (v = strtok_r(NULL, " ", &sp))) btime = atoll(v);
        else if (!strcmp(t, "winc") && (v = strtok_r(NULL, " ", &sp))) winc = atoll(v);
        else if (!strcmp(t, "binc") && (v = strtok_r(NULL, " ", &sp))) binc = atoll(v);
        else if (!strcmp(t, "movestogo") && (v = strtok_r(NULL, " ", &sp))) mtg = atoi(v);
        else if (!strcmp(t, "movetime") && (v = strtok_r(NULL, " ", &sp))) movetime = atoll(v);
        else if (!strcmp(t, "depth") && (v = strtok_r(NULL, " ", &sp))) depth = atoi(v);
        else if (!strcmp(t, "nodes") && (v = strtok_r(NULL, " ", &sp))) nodes_lim = atoll(v);
    }
    double soft = 1e9, hard = 1e9;
    long long mytime = (p->side == WHITE) ? wtime : btime;
    long long myinc = (p->side == WHITE) ? winc : binc;
    int inf_mode = infinite;
    if (movetime >= 0) {
        soft = hard = (movetime - move_overhead_ms) / 1000.0;
        if (soft < 0.02) soft = hard = 0.02;
    } else if (mytime >= 0) {
        double t = mytime / 1000.0, inc = myinc / 1000.0;
        double avail = t - move_overhead_ms / 1000.0;
        if (avail < 0.05) avail = 0.05;
        int ply = hn;
        int mtgo = mtg > 0 ? (mtg > 50 ? 50 : mtg) : (45 - ply / 2 > 18 ? 45 - ply / 2 : 18);
        soft = avail / mtgo + inc * 0.8;
        if (soft > avail * 0.25) soft = avail * 0.25;
        if (soft < 0.02) soft = 0.02;
        hard = soft * 3.0;
        if (hard > avail * 0.40) hard = avail * 0.40;
        if (hard < soft) hard = soft;
    } else if (!infinite && depth == 0 && nodes_lim == 0) {
        soft = hard = 1.0;
    }
    think(p, soft, hard, depth, nodes_lim, inf_mode);
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    init_all();
    Pos pos;
    set_fen(&pos, STARTFEN);
    char line[16384];
    while (fgets(line, sizeof line, stdin)) {
        size_t n = strlen(line);
        while (n && (line[n - 1] == '\n' || line[n - 1] == '\r' || line[n - 1] == ' ')) line[--n] = 0;
        if (!strcmp(line, "uci")) {
            printf("id name MackEngine\nid author MackBot\n");
            printf("option name Hash type spin default 64 min 1 max 2048\n");
            printf("option name Threads type spin default 1 min 1 max 1\n");
            printf("option name Move Overhead type spin default 100 min 0 max 10000\n");
            printf("uciok\n");
        } else if (!strcmp(line, "isready")) {
            printf("readyok\n");
        } else if (!strcmp(line, "ucinewgame")) {
            new_game();
        } else if (!strncmp(line, "setoption", 9)) {
            char *nm = strstr(line, "name "), *vl = strstr(line, " value ");
            if (nm && vl) {
                int val = atoi(vl + 7);
                if (!strncmp(nm + 5, "Hash", 4)) tt_alloc(val);
                else if (!strncmp(nm + 5, "Move Overhead", 13)) move_overhead_ms = val;
            }
        } else if (!strncmp(line, "position", 8)) {
            cmd_position(&pos, line + 8);
        } else if (!strncmp(line, "go perft", 8)) {
            int d = atoi(line + 8);
            printf("nodes %llu\n", (unsigned long long)perft(&pos, d));
        } else if (!strncmp(line, "go", 2)) {
            cmd_go(&pos, line + 2);
        } else if (!strcmp(line, "quit")) {
            break;
        } else if (!strcmp(line, "d")) {
            printf("key %llx side %d castle %d ep %d half %d eval %d\n",
                   (unsigned long long)pos.key, pos.side, pos.castle, pos.ep, pos.half, evaluate(&pos));
        }
        fflush(stdout);
    }
    return 0;
}
