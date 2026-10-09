"""
MackBot ULTIMATE
================
- Motor principal: Stockfish (UCI) si está disponible  -> >3000 ELO, gestiona el reloj solo.
- Motor de respaldo: motor propio en Python (negamax/PVS, profundización iterativa,
  tabla de transposición, null-move, LMR, killers/history, quiescence, gestión de tiempo).
- Partidas en hilos (el stream de eventos nunca se bloquea).
- Libro de aperturas Polyglot opcional (book.bin).
"""
import os
import sys
import time
import random
import shutil
import threading
import datetime
import urllib.request

import chess
import chess.engine
import chess.polyglot

# ======================================================================
# CONFIGURACIÓN (variables de entorno)
# ======================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOOK_PATH = os.environ.get("BOOK_PATH", os.path.join(BASE_DIR, "book.bin"))
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "")
SF_THREADS = int(os.environ.get("SF_THREADS", "1"))
SF_HASH = int(os.environ.get("SF_HASH", "64"))
MOVE_OVERHEAD = int(os.environ.get("MOVE_OVERHEAD", "500"))   # ms de latencia de red/servidor
MAX_GAMES = int(os.environ.get("MAX_GAMES", "1"))
AUTO_CHALLENGE = os.environ.get("AUTO_CHALLENGE", "1") == "1"
RATED = os.environ.get("RATED", "1") == "1"
TC_TIME = int(os.environ.get("TC_TIME", "180"))
TC_INC = int(os.environ.get("TC_INC", "2"))
BOOK_MAX_FULLMOVES = 12


def log(*a):
    print(*a, flush=True)


# ======================================================================
# UTILIDADES
# ======================================================================
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def to_ms(x):
    """Convierte int / float / timedelta / datetime (lo que devuelva berserk) a milisegundos."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, datetime.timedelta):
        return x.total_seconds() * 1000.0
    if isinstance(x, datetime.datetime):
        if x.tzinfo is None:
            x = x.replace(tzinfo=datetime.timezone.utc)
        return (x - _EPOCH).total_seconds() * 1000.0
    try:
        return float(x)
    except Exception:
        return None


def time_budget(time_left_ms, inc_ms, ply):
    """Devuelve (tiempo_suave, tiempo_duro) en segundos."""
    t = time_left_ms / 1000.0
    inc = inc_ms / 1000.0
    avail = max(0.05, t - MOVE_OVERHEAD / 1000.0)
    moves_to_go = max(18, 45 - ply // 2)
    soft = avail / moves_to_go + inc * 0.8
    soft = max(0.05, min(soft, avail * 0.25))
    hard = max(soft, min(soft * 3.0, avail * 0.40))
    return soft, hard


# ======================================================================
# MOTOR PROPIO EN PYTHON (respaldo si no hay Stockfish)
# ======================================================================
PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING = chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN, chess.KING
VAL = {PAWN: 100, KNIGHT: 320, BISHOP: 330, ROOK: 500, QUEEN: 900, KING: 20000}
MAT_MG = {PAWN: 100, KNIGHT: 320, BISHOP: 330, ROOK: 500, QUEEN: 900, KING: 0}
MAT_EG = {PAWN: 115, KNIGHT: 310, BISHOP: 320, ROOK: 520, QUEEN: 900, KING: 0}

# Tablas (vista de blancas: índice 0 = a8)
_P = [0, 0, 0, 0, 0, 0, 0, 0,
      50, 50, 50, 50, 50, 50, 50, 50,
      10, 10, 20, 30, 30, 20, 10, 10,
      5, 5, 10, 25, 25, 10, 5, 5,
      0, 0, 0, 20, 20, 0, 0, 0,
      5, -5, -10, 0, 0, -10, -5, 5,
      5, 10, 10, -20, -20, 10, 10, 5,
      0, 0, 0, 0, 0, 0, 0, 0]
_N = [-50, -40, -30, -30, -30, -30, -40, -50,
      -40, -20, 0, 0, 0, 0, -20, -40,
      -30, 0, 10, 15, 15, 10, 0, -30,
      -30, 5, 15, 20, 20, 15, 5, -30,
      -30, 0, 15, 20, 20, 15, 0, -30,
      -30, 5, 10, 15, 15, 10, 5, -30,
      -40, -20, 0, 5, 5, 0, -20, -40,
      -50, -40, -30, -30, -30, -30, -40, -50]
_B = [-20, -10, -10, -10, -10, -10, -10, -20,
      -10, 0, 0, 0, 0, 0, 0, -10,
      -10, 0, 5, 10, 10, 5, 0, -10,
      -10, 5, 5, 10, 10, 5, 5, -10,
      -10, 0, 10, 10, 10, 10, 0, -10,
      -10, 10, 10, 10, 10, 10, 10, -10,
      -10, 5, 0, 0, 0, 0, 5, -10,
      -20, -10, -10, -10, -10, -10, -10, -20]
_R = [0, 0, 0, 0, 0, 0, 0, 0,
      5, 10, 10, 10, 10, 10, 10, 5,
      -5, 0, 0, 0, 0, 0, 0, -5,
      -5, 0, 0, 0, 0, 0, 0, -5,
      -5, 0, 0, 0, 0, 0, 0, -5,
      -5, 0, 0, 0, 0, 0, 0, -5,
      -5, 0, 0, 0, 0, 0, 0, -5,
      0, 0, 0, 5, 5, 0, 0, 0]
_Q = [-20, -10, -10, -5, -5, -10, -10, -20,
      -10, 0, 0, 0, 0, 0, 0, -10,
      -10, 0, 5, 5, 5, 5, 0, -10,
      -5, 0, 5, 5, 5, 5, 0, -5,
      0, 0, 5, 5, 5, 5, 0, -5,
      -10, 5, 5, 5, 5, 5, 0, -10,
      -10, 0, 5, 0, 0, 0, 0, -10,
      -20, -10, -10, -5, -5, -10, -10, -20]
_K_MG = [-30, -40, -40, -50, -50, -40, -40, -30,
         -30, -40, -40, -50, -50, -40, -40, -30,
         -30, -40, -40, -50, -50, -40, -40, -30,
         -30, -40, -40, -50, -50, -40, -40, -30,
         -20, -30, -30, -40, -40, -30, -30, -20,
         -10, -20, -20, -20, -20, -20, -20, -10,
         20, 20, 0, 0, 0, 0, 20, 20,
         20, 30, 10, 0, 0, 10, 30, 20]
_K_EG = [-50, -40, -30, -20, -20, -30, -40, -50,
         -30, -20, -10, 0, 0, -10, -20, -30,
         -30, -10, 20, 30, 30, 20, -10, -30,
         -30, -10, 30, 40, 40, 30, -10, -30,
         -30, -10, 30, 40, 40, 30, -10, -30,
         -30, -10, 20, 30, 30, 20, -10, -30,
         -30, -30, 0, 0, 0, 0, -30, -30,
         -50, -30, -30, -30, -30, -30, -30, -50]
_P_EG = []
for _row in (0, 90, 60, 40, 25, 15, 10, 0):
    _P_EG += [_row] * 8

_PST_MG = {PAWN: _P, KNIGHT: _N, BISHOP: _B, ROOK: _R, QUEEN: _Q, KING: _K_MG}
_PST_EG = {PAWN: _P_EG, KNIGHT: _N, BISHOP: _B, ROOK: _R, QUEEN: _Q, KING: _K_EG}

# MG[color][piece_type][square] con color True=blancas
MG = {True: {}, False: {}}
EG = {True: {}, False: {}}
for _c in (True, False):
    for _pt in range(1, 7):
        MG[_c][_pt] = [MAT_MG[_pt] + _PST_MG[_pt][(s ^ 56) if _c else s] for s in range(64)]
        EG[_c][_pt] = [MAT_EG[_pt] + _PST_EG[_pt][(s ^ 56) if _c else s] for s in range(64)]

PHASE_W = {PAWN: 0, KNIGHT: 1, BISHOP: 1, ROOK: 2, QUEEN: 4, KING: 0}
PASSED_BONUS_MG = [0, 5, 10, 20, 40, 70, 110, 0]
PASSED_BONUS_EG = [0, 10, 20, 40, 75, 120, 180, 0]
FILES = [chess.BB_FILES[f] for f in range(8)]

# Máscaras de peones pasados y escudo del rey
PASSED_MASK = {True: [0] * 64, False: [0] * 64}
SHIELD_MASK = {True: [0] * 64, False: [0] * 64}
for _sq in range(64):
    _f, _r = chess.square_file(_sq), chess.square_rank(_sq)
    for _c in (True, False):
        m = 0
        rng = range(_r + 1, 8) if _c else range(0, _r)
        for rr in rng:
            for ff in (_f - 1, _f, _f + 1):
                if 0 <= ff < 8:
                    m |= 1 << chess.square(ff, rr)
        PASSED_MASK[_c][_sq] = m
        s = 0
        step = (1, 2) if _c else (-1, -2)
        for d in step:
            rr = _r + d
            if 0 <= rr < 8:
                for ff in (_f - 1, _f, _f + 1):
                    if 0 <= ff < 8:
                        s |= 1 << chess.square(ff, rr)
        SHIELD_MASK[_c][_sq] = s

_popcount = lambda x: bin(x).count("1")


def evaluate(board: chess.Board) -> int:
    """Evaluación desde el punto de vista del bando que mueve (centipeones)."""
    mg = eg = 0
    phase = 0
    for color, sign in ((True, 1), (False, -1)):
        own_pawns = board.pieces_mask(PAWN, color)
        enemy_pawns = board.pieces_mask(PAWN, not color)
        m = e = 0
        for pt in range(1, 7):
            bb = board.pieces_mask(pt, color)
            if not bb:
                continue
            tm, te = MG[color][pt], EG[color][pt]
            for sq in chess.scan_forward(bb):
                m += tm[sq]
                e += te[sq]
                if pt == PAWN:
                    if not (PASSED_MASK[color][sq] & enemy_pawns):
                        rk = chess.square_rank(sq) if color else 7 - chess.square_rank(sq)
                        m += PASSED_BONUS_MG[rk]
                        e += PASSED_BONUS_EG[rk]
                elif pt == ROOK:
                    fm = FILES[chess.square_file(sq)]
                    if not (fm & own_pawns):
                        m += 12
                        e += 8
                        if not (fm & enemy_pawns):
                            m += 10
                            e += 6
                elif pt == KING:
                    m += 9 * _popcount(SHIELD_MASK[color][sq] & own_pawns)
            phase += PHASE_W[pt] * _popcount(bb)
        if _popcount(board.pieces_mask(BISHOP, color)) >= 2:
            m += 30
            e += 45
        # peones doblados
        for f in range(8):
            c = _popcount(own_pawns & FILES[f])
            if c > 1:
                m -= 12 * (c - 1)
                e -= 18 * (c - 1)
        mg += sign * m
        eg += sign * e
    if phase > 24:
        phase = 24
    score = (mg * phase + eg * (24 - phase)) // 24
    score += 10 if board.turn else -10  # tempo
    return score if board.turn else -score


def _tt_key(board: chess.Board) -> int:
    return hash(board._transposition_key())


INF = 10 ** 6
MATE = 100000
MAX_PLY = 100
EXACT, LOWER, UPPER = 0, 1, 2


class _Timeout(Exception):
    pass


class PyEngine:
    def __init__(self):
        self.tt = {}
        self.history = [0] * 4096
        self.killers = [[None, None] for _ in range(MAX_PLY + 2)]
        self.path = []
        self.nodes = 0
        self.hard_deadline = 0.0
        self.root_move = None

    def new_game(self):
        self.tt.clear()
        self.history = [0] * 4096

    # ---------------------------------------------------------------
    def _order(self, board, moves, tt_move, ply):
        k0, k1 = self.killers[ply]
        hist = self.history
        scored = []
        for m in moves:
            if m == tt_move:
                s = 10 ** 7
            elif board.is_capture(m):
                victim = board.piece_type_at(m.to_square) or PAWN
                att = board.piece_type_at(m.from_square)
                s = 10 ** 6 + VAL[victim] * 10 - VAL[att]
            elif m.promotion:
                s = 900000 + m.promotion
            elif m == k0:
                s = 800000
            elif m == k1:
                s = 700000
            else:
                s = min(hist[m.from_square * 64 + m.to_square], 600000)
            scored.append((s, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored]

    # ---------------------------------------------------------------
    def _qsearch(self, board, alpha, beta, ply):
        self.nodes += 1
        if (self.nodes & 1023) == 0 and time.time() > self.hard_deadline:
            raise _Timeout
        if ply >= MAX_PLY:
            return evaluate(board)

        in_check = board.is_check()
        if in_check:
            best = -MATE + ply
            moves = list(board.legal_moves)
            if not moves:
                return best
            stand = -INF
        else:
            stand = evaluate(board)
            if stand >= beta:
                return stand
            if stand > alpha:
                alpha = stand
            best = stand
            moves = list(board.generate_legal_captures())

        def mvv(m):
            v = board.piece_type_at(m.to_square)
            if v is None:
                return 0 if not board.is_en_passant(m) else 10 * 100 - 100
            return 10 * VAL[v] - VAL[board.piece_type_at(m.from_square)]

        moves.sort(key=mvv, reverse=True)
        opp = not board.turn
        for m in moves:
            if not in_check and not m.promotion:
                victim = board.piece_type_at(m.to_square) or PAWN
                if stand + VAL[victim] + 200 < alpha:
                    continue  # delta pruning
                att = board.piece_type_at(m.from_square)
                if VAL[att] > VAL[victim] + 50 and board.is_attacked_by(opp, m.to_square):
                    continue  # captura claramente perdedora
            board.push(m)
            score = -self._qsearch(board, -beta, -alpha, ply + 1)
            board.pop()
            if score > best:
                best = score
                if score > alpha:
                    alpha = score
                    if alpha >= beta:
                        break
        return best

    # ---------------------------------------------------------------
    def _search(self, board, depth, alpha, beta, ply, allow_null):
        self.nodes += 1
        if (self.nodes & 1023) == 0 and time.time() > self.hard_deadline:
            raise _Timeout

        is_pv = (beta - alpha) > 1
        key = _tt_key(board)

        if ply > 0:
            hm = board.halfmove_clock
            if hm >= 100:
                return 0
            if hm >= 4 and key in self.path[-hm:]:
                return 0

        in_check = board.is_check()
        if in_check:
            depth += 1
        if depth <= 0:
            return self._qsearch(board, alpha, beta, ply)
        if ply >= MAX_PLY - 1:
            return evaluate(board)

        # --- TT ---
        tt_move = None
        entry = self.tt.get(key)
        if entry is not None:
            e_depth, flag, e_score, tt_move = entry
            if ply > 0 and e_depth >= depth and not is_pv:
                if e_score > MATE - 1000:
                    e_score -= ply
                elif e_score < -MATE + 1000:
                    e_score += ply
                if flag == EXACT:
                    return e_score
                if flag == LOWER and e_score >= beta:
                    return e_score
                if flag == UPPER and e_score <= alpha:
                    return e_score

        static = None
        if not in_check and not is_pv and ply > 0:
            static = evaluate(board)
            # reverse futility
            if depth <= 3 and static - 120 * depth >= beta:
                return static
            # null move
            if (allow_null and depth >= 3 and static >= beta and
                    board.occupied_co[board.turn] & ~(board.pawns | board.kings)):
                R = 2 + (1 if depth >= 6 else 0)
                self.path.append(key)
                board.push(chess.Move.null())
                score = -self._search(board, depth - 1 - R, -beta, -beta + 1, ply + 1, False)
                board.pop()
                self.path.pop()
                if score >= beta:
                    return beta if score >= MATE - 1000 else score

        moves = list(board.legal_moves)
        if not moves:
            return (-MATE + ply) if in_check else 0
        moves = self._order(board, moves, tt_move, ply)

        orig_alpha = alpha
        best = -INF
        best_move = None
        self.path.append(key)
        for i, m in enumerate(moves):
            is_quiet = not board.is_capture(m) and not m.promotion

            # futility pruning
            if (is_quiet and i > 0 and static is not None and depth <= 2 and
                    static + 150 * depth <= alpha and best > -MATE + 1000):
                continue

            board.push(m)
            gives_check = board.is_check()
            if i == 0:
                score = -self._search(board, depth - 1, -beta, -alpha, ply + 1, True)
            else:
                r = 0
                if depth >= 3 and i >= 3 and is_quiet and not in_check and not gives_check:
                    r = 1 + (1 if i >= 6 else 0) + (1 if (depth >= 6 and i >= 12) else 0)
                    r = min(r, depth - 2)
                score = -self._search(board, depth - 1 - r, -alpha - 1, -alpha, ply + 1, True)
                if score > alpha and r > 0:
                    score = -self._search(board, depth - 1, -alpha - 1, -alpha, ply + 1, True)
                if alpha < score < beta:
                    score = -self._search(board, depth - 1, -beta, -alpha, ply + 1, True)
            board.pop()

            if score > best:
                best = score
                best_move = m
                if score > alpha:
                    alpha = score
                    if ply == 0:
                        self.root_move = m
                    if alpha >= beta:
                        if is_quiet:
                            ks = self.killers[ply]
                            if ks[0] != m:
                                ks[1] = ks[0]
                                ks[0] = m
                            self.history[m.from_square * 64 + m.to_square] += depth * depth
                        break
        self.path.pop()

        if best <= orig_alpha:
            flag = UPPER
        elif best >= beta:
            flag = LOWER
        else:
            flag = EXACT
        stored = best
        if stored > MATE - 1000:
            stored += ply
        elif stored < -MATE + 1000:
            stored -= ply
        if len(self.tt) > 800_000:
            self.tt.clear()
        self.tt[key] = (depth, flag, stored, best_move)
        return best

    # ---------------------------------------------------------------
    def think(self, board: chess.Board, soft: float, hard: float, max_depth: int = 40):
        """Devuelve (mejor_jugada, puntuación_cp, profundidad, nodos)."""
        start = time.time()
        self.hard_deadline = start + hard
        self.nodes = 0
        self.history = [h // 2 for h in self.history]
        self.killers = [[None, None] for _ in range(MAX_PLY + 2)]

        root = board.copy()
        legal = list(root.legal_moves)
        if len(legal) == 1:
            return legal[0], 0, 0, 0

        # historial de claves (para detectar repeticiones con la partida real)
        tmp = board.copy()
        keys = []
        while tmp.move_stack:
            tmp.pop()
            keys.append(_tt_key(tmp))
        keys.reverse()
        self.path = keys

        best_move, best_score, reached = legal[0], 0, 0
        score = 0
        for depth in range(1, max_depth + 1):
            try:
                self.root_move = None
                if depth >= 5:
                    delta = 35
                    alpha, beta = score - delta, score + delta
                    while True:
                        self.path = keys[:]
                        s = self._search(root, depth, alpha, beta, 0, False)
                        if s <= alpha:
                            alpha = -INF if alpha < -1000 else alpha - delta * 3
                        elif s >= beta:
                            beta = INF if beta > 1000 else beta + delta * 3
                        else:
                            break
                        delta *= 2
                    score = s
                else:
                    self.path = keys[:]
                    score = self._search(root, depth, -INF, INF, 0, False)
            except _Timeout:
                break
            if self.root_move is not None:
                best_move, best_score, reached = self.root_move, score, depth
            elapsed = time.time() - start
            if abs(score) > MATE - 100:
                break
            if elapsed > soft * 0.5:
                break
        return best_move, best_score, reached, self.nodes


# ======================================================================
# SELECCIÓN DE MOVIMIENTO: libro -> Stockfish -> motor propio
# ======================================================================
def find_stockfish():
    cands = [STOCKFISH_PATH,
             shutil.which("stockfish") or "",
             "/usr/games/stockfish", "/usr/bin/stockfish", "/usr/local/bin/stockfish",
             os.path.join(BASE_DIR, "stockfish")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


class Brain:
    def __init__(self):
        self.lock = threading.Lock()
        self.py = PyEngine()
        self.sf = None
        self.sf_path = find_stockfish()
        self._start_stockfish()

    def _start_stockfish(self):
        if not self.sf_path:
            log("⚠️  Stockfish NO encontrado: usando motor propio de Python (mucho más débil).")
            return
        try:
            self.sf = chess.engine.SimpleEngine.popen_uci(self.sf_path)
            opts = {"Threads": SF_THREADS, "Hash": SF_HASH, "Move Overhead": MOVE_OVERHEAD}
            cfg = {k: v for k, v in opts.items() if k in self.sf.options}
            self.sf.configure(cfg)
            log(f"♟️  Stockfish listo: {self.sf.id.get('name', 'Stockfish')} ({self.sf_path}) {cfg}")
        except Exception as e:
            log(f"⚠️  No se pudo iniciar Stockfish ({e}). Usando motor propio.")
            self.sf = None

    def new_game(self):
        with self.lock:
            self.py.new_game()
            if self.sf:
                try:
                    self.sf.protocol.send_line("ucinewgame")
                except Exception:
                    pass

    @staticmethod
    def book_move(board):
        if board.fullmove_number > BOOK_MAX_FULLMOVES or not os.path.exists(BOOK_PATH):
            return None
        try:
            with chess.polyglot.open_reader(BOOK_PATH) as r:
                return r.weighted_choice(board).move
        except (IndexError, Exception):
            return None

    def choose(self, board, wtime, btime, winc, binc):
        bm = self.book_move(board)
        if bm and bm in board.legal_moves:
            log("  📖 libro")
            return bm

        legal = list(board.legal_moves)
        if len(legal) == 1:
            return legal[0]

        with self.lock:
            if self.sf:
                try:
                    wt = (wtime if wtime is not None else 60000) / 1000.0
                    bt = (btime if btime is not None else 60000) / 1000.0
                    limit = chess.engine.Limit(
                        white_clock=wt, black_clock=bt,
                        white_inc=(winc or 0) / 1000.0, black_inc=(binc or 0) / 1000.0)
                    res = self.sf.play(board, limit)
                    if res.move:
                        return res.move
                except Exception as e:
                    log(f"⚠️  Stockfish falló ({e}); reiniciando y usando respaldo.")
                    try:
                        self.sf.quit()
                    except Exception:
                        pass
                    self.sf = None
                    self._start_stockfish()

            my_t = wtime if board.turn == chess.WHITE else btime
            my_inc = winc if board.turn == chess.WHITE else binc
            if my_t is None:
                my_t = 300000.0
            soft, hard = time_budget(my_t, my_inc or 0, board.ply())
            mv, sc, d, n = self.py.think(board, soft, hard)
            log(f"  🧠 propio: prof {d} | {sc:+d}cp | {n} nodos | soft {soft:.2f}s")
            return mv


# ======================================================================
# LICHESS
# ======================================================================
active_games = set()
games_lock = threading.Lock()


def send_move(client, game_id, move, attempts=6):
    """Envía la jugada con reintentos ante fallos de red. Devuelve True si quedó enviada."""
    for i in range(attempts):
        try:
            client.bots.make_move(game_id, move.uci())
            return True
        except Exception as e:
            code = getattr(e, "status_code", None)
            if code in (400, 404):
                # Lichess la rechazó: normalmente ya se había registrado (o no es nuestro turno)
                log(f"  ℹ️  Lichess respondió {code} al enviar {move.uci()} (probablemente ya aplicada)")
                return True
            log(f"⚠️  Error enviando jugada (intento {i + 1}/{attempts}): {e}")
            time.sleep(min(0.25 * (i + 1), 1.5))
    return False


def play_game(client, brain, my_id, game_id):
    log(f"🎮 Partida: https://lichess.org/{game_id}")
    brain.new_game()
    my_color = None
    initial = "startpos"
    last_len = -1
    finished = False
    reconnects = 0
    try:
        while not finished and reconnects < 30:
            try:
                for ev in client.bots.stream_game_state(game_id):
                    t = ev.get("type")
                    if t == "gameFull":
                        my_color = chess.WHITE if ev["white"].get("id") == my_id else chess.BLACK
                        initial = ev.get("initialFen", "startpos")
                        state = ev["state"]
                    elif t == "gameState":
                        state = ev
                    else:
                        continue

                    if state.get("status") not in ("started", "created"):
                        log(f"🏁 Fin de {game_id}: {state.get('status')}")
                        finished = True
                        break

                    board = chess.Board() if initial in (None, "startpos") else chess.Board(initial)
                    for u in (state.get("moves") or "").split():
                        board.push_uci(u)

                    if board.is_game_over() or board.turn != my_color:
                        continue
                    if len(board.move_stack) == last_len:
                        continue  # ya movimos en esta posición

                    wtime, btime = to_ms(state.get("wtime")), to_ms(state.get("btime"))
                    winc, binc = to_ms(state.get("winc")), to_ms(state.get("binc"))

                    t0 = time.time()
                    try:
                        move = brain.choose(board, wtime, btime, winc, binc)
                    except Exception as e:
                        log(f"⚠️  Error calculando ({e}); jugada de emergencia.")
                        move = random.choice(list(board.legal_moves))

                    if send_move(client, game_id, move):
                        last_len = len(board.move_stack)
                        log(f"  👉 {move.uci()} ({time.time() - t0:.2f}s)")
                    else:
                        # No se pudo enviar: forzamos reconectar el stream, que reenvía el estado
                        # completo (gameFull) y nos da otra oportunidad de mover.
                        raise ConnectionError("no se pudo enviar la jugada tras varios intentos")
                else:
                    # el stream terminó limpiamente: confirmamos si la partida sigue viva
                    pass
            except Exception as e:
                reconnects += 1
                log(f"🔄 Stream de {game_id} cortado ({e}); reconectando ({reconnects})...")
                time.sleep(min(0.5 * reconnects, 3))
                continue
            if not finished:
                # el stream se cerró sin fin de partida: ¿sigue en curso?
                try:
                    info = client.games.export(game_id)
                    if info.get("status") not in ("started", "created"):
                        finished = True
                except Exception:
                    pass
                reconnects += 1
                time.sleep(1)
    finally:
        with games_lock:
            active_games.discard(game_id)


def start_game_thread(client, brain, my_id, game_id):
    with games_lock:
        if game_id in active_games:
            return
        active_games.add(game_id)
    threading.Thread(target=play_game, args=(client, brain, my_id, game_id), daemon=True).start()


def auto_challenge_loop(client, my_id, my_rating):
    log("🤖 Auto-desafíos activados")
    time.sleep(15)
    cooldown = {}
    while True:
        try:
            with games_lock:
                busy = len(active_games) >= MAX_GAMES
            if busy:
                time.sleep(15)
                continue
            now = time.time()
            bots = []
            for i, b in enumerate(client.bots.get_online_bots()):
                if i > 300:
                    break
                bid = b.get("id")
                if not bid or bid == my_id or b.get("disabled"):
                    continue
                if cooldown.get(bid, 0) > now:
                    continue
                r = (b.get("perfs", {}).get("blitz", {}) or {}).get("rating", 1500)
                bots.append((abs(r - my_rating), bid))
            if not bots:
                time.sleep(30)
                continue
            bots.sort()
            pool = [b for _, b in bots[:25]]
            target = random.choice(pool)
            cooldown[target] = now + 600
            log(f"⚔️  Retando a {target} ({'clasificada' if RATED else 'amistosa'})")
            try:
                client.challenges.create(username=target, rated=RATED,
                                         clock_limit=TC_TIME, clock_increment=TC_INC, color="random")
            except Exception as e:
                log(f"   (reto fallido: {e})")
            time.sleep(60)
        except Exception as e:
            log(f"auto_challenge error: {e}")
            time.sleep(60)


def keep_alive_loop():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(url, timeout=15).read()
        except Exception:
            pass


def start_http_server():
    from flask import Flask
    app = Flask(__name__)

    @app.route("/")
    @app.route("/health")
    def home():
        return "MackBot ULTIMATE online", 200

    port = int(os.environ.get("PORT", 10000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()


def main():
    import berserk
    start_http_server()
    threading.Thread(target=keep_alive_loop, daemon=True).start()

    token = os.environ.get("LICHESS_TOKEN")
    if not token:
        raise SystemExit("⚠️ Falta LICHESS_TOKEN")
    client = berserk.Client(session=berserk.TokenSession(token))
    profile = client.account.get()
    my_id, my_name = profile["id"], profile["username"]
    my_rating = (profile.get("perfs", {}).get("blitz", {}) or {}).get("rating", 1500)
    log(f"✅ Conectado como {my_name} (blitz {my_rating})")

    brain = Brain()

    if AUTO_CHALLENGE:
        threading.Thread(target=auto_challenge_loop, args=(client, my_id, my_rating), daemon=True).start()

    # reanudar partidas en curso tras un reinicio
    try:
        for g in client.games.get_ongoing(limit=5):
            start_game_thread(client, brain, my_id, g["gameId"])
    except Exception:
        pass

    while True:
        try:
            for event in client.bots.stream_incoming_events():
                et = event.get("type")
                if et == "challenge":
                    ch = event["challenge"]
                    cid = ch["id"]
                    challenger = (ch.get("challenger") or {}).get("id")
                    if challenger == my_id:
                        continue
                    variant = ch.get("variant", {}).get("key")
                    speed = ch.get("speed")
                    with games_lock:
                        busy = len(active_games) >= MAX_GAMES
                    try:
                        if variant != "standard":
                            client.bots.decline_challenge(cid, reason="standard")
                        elif speed == "correspondence":
                            client.bots.decline_challenge(cid, reason="timeControl")
                        elif busy:
                            client.bots.decline_challenge(cid, reason="later")
                        else:
                            client.bots.accept_challenge(cid)
                            log(f"🤝 Reto aceptado de {challenger}")
                    except Exception as e:
                        log(f"(reto {cid}: {e})")
                elif et == "gameStart":
                    start_game_thread(client, brain, my_id, event["game"]["gameId"])
        except Exception as err:
            log(f"🔌 Stream de eventos caído, reconectando en 5s... ({err})")
            time.sleep(5)


if __name__ == "__main__":
    main()
