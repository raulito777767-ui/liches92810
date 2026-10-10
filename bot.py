"""
MackBot ULTIMATE (motor en C)
=============================
- Motor: MackEngine (engine.c, compilado a ./mackengine), hablado por UCI.
  Gestiona el reloj solo y usa ~70 MB de RAM con Hash=64.
- Partidas en hilos (el stream de eventos nunca se bloquea).
- Libro de aperturas Polyglot opcional (book.bin).
- El servidor Flask solo existe para que Render y tu cron tengan a quien pingear.
"""
import os
import time
import random
import shutil
import subprocess
import threading
import datetime

import chess
import chess.engine
import chess.polyglot

# ======================================================================
# CONFIGURACIÓN (variables de entorno)
# ======================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOOK_PATH = os.environ.get("BOOK_PATH", os.path.join(BASE_DIR, "book.bin"))
ENGINE_SRC = os.path.join(BASE_DIR, "engine.c")
ENGINE_PATH = os.environ.get("ENGINE_PATH", os.path.join(BASE_DIR, "mackengine"))
ENGINE_HASH = int(os.environ.get("ENGINE_HASH", "64"))        # MB de tabla de transposición
MOVE_OVERHEAD = int(os.environ.get("MOVE_OVERHEAD", "500"))   # ms de latencia de red/servidor
MAX_GAMES = int(os.environ.get("MAX_GAMES", "1"))
AUTO_CHALLENGE = os.environ.get("AUTO_CHALLENGE", "1") == "1"
RATED = os.environ.get("RATED", "1") == "1"
# Modos de los auto-desafíos: lista separada por comas de bullet, blitz, rapid, classical.
# Ej.: CHALLENGE_MODES=bullet,blitz,rapid,classical   (por defecto solo blitz)
CHALLENGE_MODES = os.environ.get("CHALLENGE_MODES", "blitz")
# Opcional: ritmos exactos "segundos+incremento", p. ej. CHALLENGE_TCS=60+0,180+2,600+5,1800+0
# Si se define, tiene prioridad sobre CHALLENGE_MODES.
CHALLENGE_TCS = os.environ.get("CHALLENGE_TCS", "").strip()
TC_PRESETS = {
    "bullet": [(60, 0), (60, 1), (120, 1)],
    "blitz": [(180, 0), (180, 2), (300, 0), (300, 3)],
    "rapid": [(600, 0), (600, 5), (900, 10)],
    "classical": [(1800, 0), (1800, 20)],
}
CHALLENGE_INTERVAL = int(os.environ.get("CHALLENGE_INTERVAL", "90"))        # s entre retos
MAX_CHALLENGES_PER_HOUR = int(os.environ.get("MAX_CHALLENGES_PER_HOUR", "20"))
BOT_LIST_TTL = int(os.environ.get("BOT_LIST_TTL", "300"))                    # s de caché de la lista de bots
LOG_ENGINE = os.environ.get("LOG_ENGINE", "1") == "1"        # mostrar prof/eval/nodos en los logs
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


# ======================================================================
# MOTOR EN C
# ======================================================================
def ensure_engine():
    """Devuelve la ruta del binario; si no existe intenta compilarlo con gcc."""
    if os.path.isfile(ENGINE_PATH) and os.access(ENGINE_PATH, os.X_OK):
        return ENGINE_PATH
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc and os.path.isfile(ENGINE_SRC):
        log(f"🔧 Compilando el motor con {cc}...")
        try:
            subprocess.run([cc, "-O3", "-o", ENGINE_PATH, ENGINE_SRC, "-lm"],
                           check=True, capture_output=True, text=True, timeout=180)
            return ENGINE_PATH
        except Exception as e:
            err = getattr(e, "stderr", "") or str(e)
            log(f"❌ Falló la compilación: {err[:500]}")
    return None


class Brain:
    def __init__(self):
        self.lock = threading.Lock()
        self.engine = None
        self.path = ensure_engine()
        if not self.path:
            raise SystemExit("❌ No hay binario del motor (mackengine) ni gcc para compilarlo.")
        self._start_engine()
        if self.engine is None:
            raise SystemExit("❌ No se pudo iniciar el motor.")

    def _start_engine(self):
        try:
            self.engine = chess.engine.SimpleEngine.popen_uci(self.path, timeout=60)
            cfg = {"Hash": ENGINE_HASH, "Move Overhead": MOVE_OVERHEAD}
            cfg = {k: v for k, v in cfg.items() if k in self.engine.options}
            self.engine.configure(cfg)
            log(f"♟️  Motor listo: {self.engine.id.get('name', 'MackEngine')} {cfg}")
        except Exception as e:
            log(f"⚠️  No se pudo iniciar el motor ({e}).")
            self.engine = None

    def _kill_engine(self):
        try:
            if self.engine:
                self.engine.quit()
        except Exception:
            pass
        self.engine = None

    @staticmethod
    def book_move(board):
        if board.fullmove_number > BOOK_MAX_FULLMOVES or not os.path.exists(BOOK_PATH):
            return None
        try:
            with chess.polyglot.open_reader(BOOK_PATH) as r:
                return r.weighted_choice(board).move
        except (IndexError, Exception):
            return None

    @staticmethod
    def _log_info(info):
        """Escribe profundidad, evaluación (desde el punto de vista del bot) y nodos."""
        if not LOG_ENGINE or not info:
            return
        try:
            parts = []
            if "depth" in info:
                parts.append(f"prof {info['depth']}")
            sc = info.get("score")
            if sc is not None:
                rel = sc.relative
                parts.append(f"#{rel.mate()}" if rel.is_mate() else f"{rel.score() / 100:+.2f}")
            if "nodes" in info:
                parts.append(f"{info['nodes']:,} nodos")
            if "nps" in info:
                parts.append(f"{info['nps']:,} n/s")
            if parts:
                log("  🧠 " + " | ".join(parts))
        except Exception:
            pass

    def choose(self, board, wtime, btime, winc, binc, game_id=None):
        bm = self.book_move(board)
        if bm and bm in board.legal_moves:
            log("  📖 libro")
            return bm

        legal = list(board.legal_moves)
        if len(legal) == 1:
            log("  ♟️  jugada única")
            return legal[0]

        wt = (wtime if wtime is not None else 60000) / 1000.0
        bt = (btime if btime is not None else 60000) / 1000.0
        limit = chess.engine.Limit(
            white_clock=wt, black_clock=bt,
            white_inc=(winc or 0) / 1000.0, black_inc=(binc or 0) / 1000.0)

        with self.lock:
            for _ in range(2):
                try:
                    if self.engine is None:
                        self._start_engine()
                    if self.engine is not None:
                        res = self.engine.play(board, limit, game=game_id, info=chess.engine.INFO_ALL)
                        if res.move and res.move in board.legal_moves:
                            self._log_info(res.info)
                            return res.move
                except Exception as e:
                    log(f"⚠️  El motor falló ({e}); reiniciándolo.")
                    self._kill_engine()
        log("⚠️  Sin respuesta del motor: jugada de emergencia.")
        return random.choice(legal)


def speed_of(limit, inc):
    """Clasificación de Lichess según la duración estimada (límite + 40 * incremento)."""
    est = limit + 40 * inc
    if est < 30:
        return "ultraBullet"
    if est < 180:
        return "bullet"
    if est < 480:
        return "blitz"
    if est < 1500:
        return "rapid"
    return "classical"


def build_time_controls():
    tcs = []
    if CHALLENGE_TCS:
        for item in CHALLENGE_TCS.split(","):
            try:
                a, b = item.strip().split("+")
                tcs.append((int(a), int(b)))
            except ValueError:
                log(f"⚠️  Ritmo inválido en CHALLENGE_TCS: '{item}' (usa segundos+incremento)")
    else:
        for m in CHALLENGE_MODES.split(","):
            m = m.strip().lower()
            if m in TC_PRESETS:
                tcs += TC_PRESETS[m]
            elif m:
                log(f"⚠️  Modo desconocido en CHALLENGE_MODES: '{m}'")
    return tcs or list(TC_PRESETS["blitz"])


def rating_of(perfs, speed):
    return ((perfs or {}).get(speed) or {}).get("rating", 1500)



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
                        move = brain.choose(board, wtime, btime, winc, binc, game_id)
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


def _is_429(e):
    return getattr(e, "status_code", None) == 429 or "429" in str(e)


def auto_challenge_loop(client, my_id, my_perfs):
    tcs = build_time_controls()
    by_speed = {}
    for tc in tcs:
        by_speed.setdefault(speed_of(*tc), []).append(tc)
    log(f"🤖 Auto-desafíos activados: " +
        ", ".join(f"{sp} {['%d+%d' % t for t in v]}" for sp, v in by_speed.items()))
    time.sleep(20)
    cooldown = {}
    sent = []              # marcas de tiempo de retos enviados (última hora)
    bots_raw, cache_ts = [], 0.0
    backoff = 0
    while True:
        try:
            with games_lock:
                busy = len(active_games) >= MAX_GAMES
            if busy:
                time.sleep(15)
                continue

            now = time.time()
            sent[:] = [t for t in sent if now - t < 3600]
            if len(sent) >= MAX_CHALLENGES_PER_HOUR:
                time.sleep(60)
                continue

            # lista de bots: se refresca como mucho cada BOT_LIST_TTL segundos
            if not bots_raw or now - cache_ts > BOT_LIST_TTL:
                fresh = []
                for i, b in enumerate(client.bots.get_online_bots()):
                    if i > 300:
                        break
                    bid = b.get("id")
                    if not bid or bid == my_id or b.get("disabled"):
                        continue
                    fresh.append((bid, b.get("perfs", {}) or {}))
                bots_raw, cache_ts = fresh, time.time()

            # primero el modo (uniforme entre los activados), luego el ritmo concreto
            speed = random.choice(list(by_speed))
            limit, inc = random.choice(by_speed[speed])
            my_r = rating_of(my_perfs, speed)
            ranked = sorted((abs(rating_of(perfs, speed) - my_r), bid) for bid, perfs in bots_raw)
            pool = [bid for _, bid in ranked[:40] if cooldown.get(bid, 0) < now]
            if not pool:
                time.sleep(30)
                continue

            target = random.choice(pool)
            cooldown[target] = now + 900
            log(f"⚔️  Retando a {target} ({speed} {limit}+{inc}, {'clasificada' if RATED else 'amistosa'})")
            client.challenges.create(username=target, rated=RATED,
                                     clock_limit=limit, clock_increment=inc, color="random")
            sent.append(time.time())
            backoff = 0
            time.sleep(CHALLENGE_INTERVAL + random.uniform(0, 20))
        except Exception as e:
            if _is_429(e):
                backoff = min(backoff * 2 if backoff else 120, 900)
                log(f"⏳ Lichess pide frenar (429). Pausa de {backoff}s en los auto-desafíos.")
                time.sleep(backoff)
            else:
                log(f"   (reto fallido: {e})")
                time.sleep(CHALLENGE_INTERVAL)


def start_http_server():
    """Endpoint mínimo para que Render (y tu cron) tengan a quién pingear."""
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

    token = os.environ.get("LICHESS_TOKEN")
    if not token:
        raise SystemExit("⚠️ Falta LICHESS_TOKEN")
    client = berserk.Client(session=berserk.TokenSession(token))
    profile = client.account.get()
    my_id, my_name = profile["id"], profile["username"]
    my_perfs = profile.get("perfs", {}) or {}
    log(f"✅ Conectado como {my_name} (blitz {rating_of(my_perfs, 'blitz')})")

    brain = Brain()

    if AUTO_CHALLENGE:
        threading.Thread(target=auto_challenge_loop, args=(client, my_id, my_perfs), daemon=True).start()

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
