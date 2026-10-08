import os
import time
import math
import random
import threading
import datetime
from flask import Flask
import chess
import chess.polyglot
import berserk

# ==========================================
# 1. SERVIDOR HTTP (Evita que Render se duerma)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "MackBot Engine (>2000 ELO) está online y jugando en Lichess.", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ==========================================
# 2. MOTOR DE AJEDREZ, CACHÉ Y EVALUACIÓN
# ==========================================

BOOK_PATH = os.path.join(os.path.dirname(__file__), "book.bin")

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000
}

# Tablas de Posición (Piece-Square Tables)
pawntable = [
    0,  0,  0,  0,  0,  0,  0,  0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
     5,  5, 10, 25, 25, 10,  5,  5,
     0,  0,  0, 20, 20,  0,  0,  0,
     5, -5,-10,  0,  0,-10, -5,  5,
     5, 10, 10,-20,-20, 10, 10,  5,
     0,  0,  0,  0,  0,  0,  0,  0
]
knightstable = [
    -50,-40,-30,-30,-30,-30,-40,-50,
    -40,-20,  0,  0,  0,  0,-20,-40,
    -30,  0, 10, 15, 15, 10,  0,-30,
    -30,  5, 15, 20, 20, 15,  5,-30,
    -30,  0, 15, 20, 20, 15,  0,-30,
    -30,  5, 10, 15, 15, 10,  5,-30,
    -40,-20,  0,  5,  5,  0,-20,-40,
    -50,-40,-30,-30,-30,-30,-40,-50,
]
bishopstable = [
    -20,-10,-10,-10,-10,-10,-10,-20,
    -10,  0,  0,  0,  0,  0,  0,-10,
    -10,  0,  5, 10, 10,  5,  0,-10,
    -10,  5,  5, 10, 10,  5,  5,-10,
    -10,  0, 10, 10, 10, 10,  0,-10,
    -10, 10, 10, 10, 10, 10, 10,-10,
    -10,  5,  0,  0,  0,  0,  5,-10,
    -20,-10,-10,-10,-10,-10,-10,-20,
]
rookstable = [
      0,  0,  0,  0,  0,  0,  0,  0,
      5, 10, 10, 10, 10, 10, 10,  5,
     -5,  0,  0,  0,  0,  0,  0, -5,
     -5,  0,  0,  0,  0,  0,  0, -5,
     -5,  0,  0,  0,  0,  0,  0, -5,
     -5,  0,  0,  0,  0,  0,  0, -5,
     -5,  0,  0,  0,  0,  0,  0, -5,
      0,  0,  0,  5,  5,  0,  0,  0
]
queenstable = [
    -20,-10,-10, -5, -5,-10,-10,-20,
    -10,  0,  0,  0,  0,  0,  0,-10,
    -10,  0,  5,  5,  5,  5,  0,-10,
     -5,  0,  5,  5,  5,  5,  0, -5,
      0,  0,  5,  5,  5,  5,  0, -5,
    -10,  5,  5,  5,  5,  5,  0,-10,
    -10,  0,  5,  0,  0,  0,  0,-10,
    -20,-10,-10, -5, -5,-10,-10,-20
]
kingstable = [
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -30,-40,-40,-50,-50,-40,-40,-30,
    -20,-30,-30,-40,-40,-30,-30,-20,
    -10,-20,-20,-20,-20,-20,-20,-10,
     20, 20,  0,  0,  0,  0, 20, 20,
     20, 30, 10,  0,  0, 10, 30, 20
]

# Caché de Transposición para acelerar el Minimax sin perder profundidad
transposition_table = {}

def evaluate_board(board: chess.Board) -> int:
    if board.is_checkmate():
        return -99999 if board.turn == chess.WHITE else 99999
    if board.is_stalemate() or board.is_insufficient_material() or board.can_claim_threefold_repetition():
        return 0

    evaluation = 0
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is not None:
            val = PIECE_VALUES[piece.piece_type]
            sq = square if piece.color == chess.WHITE else chess.square_mirror(square)
            
            if piece.piece_type == chess.PAWN: val += pawntable[sq]
            elif piece.piece_type == chess.KNIGHT: val += knightstable[sq]
            elif piece.piece_type == chess.BISHOP: val += bishopstable[sq]
            elif piece.piece_type == chess.ROOK: val += rookstable[sq]
            elif piece.piece_type == chess.QUEEN: val += queenstable[sq]
            elif piece.piece_type == chess.KING: val += kingstable[sq]

            evaluation += val if piece.color == chess.WHITE else -val

    return evaluation

def order_moves(board: chess.Board, moves):
    def score_move(move):
        score = 0
        if board.is_capture(move):
            attacker = board.piece_at(move.from_square)
            victim = board.piece_at(move.to_square)
            if attacker and victim:
                score += 10 * PIECE_VALUES[victim.piece_type] - PIECE_VALUES[attacker.piece_type]
                # Penalización severa para evitar colgar piezas en casillas defendidas
                if PIECE_VALUES[attacker.piece_type] > PIECE_VALUES[victim.piece_type]:
                    if board.is_attacked_by(not board.turn, move.to_square):
                        score -= 2000
            else:
                score += 500
        if board.gives_check(move):
            score += 300
        return score

    return sorted(moves, key=score_move, reverse=True)

def quiescence_search(board: chess.Board, alpha: int, beta: int) -> int:
    stand_pat = evaluate_board(board)
    if board.turn == chess.BLACK:
        stand_pat = -stand_pat

    if stand_pat >= beta:
        return beta
    if alpha < stand_pat:
        alpha = stand_pat

    captures = [m for m in board.legal_moves if board.is_capture(m)]
    captures = order_moves(board, captures)

    for move in captures:
        board.push(move)
        score = -quiescence_search(board, -beta, -alpha)
        board.pop()

        if score >= beta:
            return beta
        if score > alpha:
            alpha = score

    return alpha

def minimax(board: chess.Board, depth: int, alpha: int, beta: int, is_maximizing: bool) -> int:
    board_fen = board.fen()
    cache_key = (board_fen, depth, is_maximizing)
    
    if cache_key in transposition_table:
        return transposition_table[cache_key]

    if depth == 0 or board.is_game_over():
        val = quiescence_search(board, alpha, beta) if is_maximizing else -quiescence_search(board, -beta, -alpha)
        transposition_table[cache_key] = val
        return val

    legal_moves = order_moves(board, list(board.legal_moves))

    if is_maximizing:
        max_eval = -math.inf
        for move in legal_moves:
            board.push(move)
            eval = minimax(board, depth - 1, alpha, beta, False)
            board.pop()
            max_eval = max(max_eval, eval)
            alpha = max(alpha, eval)
            if beta <= alpha: break
        transposition_table[cache_key] = max_eval
        return max_eval
    else:
        min_eval = math.inf
        for move in legal_moves:
            board.push(move)
            eval = minimax(board, depth - 1, alpha, beta, True)
            board.pop()
            min_eval = min(min_eval, eval)
            beta = min(beta, eval)
            if beta <= alpha: break
        transposition_table[cache_key] = min_eval
        return min_eval

def get_opening_move_polyglot(board: chess.Board) -> chess.Move | None:
    if not os.path.exists(BOOK_PATH):
        return None
    try:
        with chess.polyglot.open_reader(BOOK_PATH) as reader:
            entries = list(reader.find_all(board))
            if entries: return random.choice(entries).move
    except Exception as e:
        print(f"Error libro de aperturas: {e}", flush=True)
    return None

def calculate_dynamic_depth(board: chess.Board, my_time_ms) -> int:
    if isinstance(my_time_ms, datetime.timedelta):
        time_left_sec = my_time_ms.total_seconds()
    else:
        time_left_sec = float(my_time_ms) / 1000.0 if my_time_ms else 180.0

    if time_left_sec < 15:
        return 2  # Apuros extremos
    else:
        return 3  # Profundidad óptima y rápida con caché

def get_best_move(board: chess.Board, my_time_ms: int = 180000) -> chess.Move:
    if board.fullmove_number <= 15:
        book_move = get_opening_move_polyglot(board)
        if book_move:
            print("  📖 Jugada de libro (book.bin)", flush=True)
            return book_move

    depth = calculate_dynamic_depth(board, my_time_ms)
    print(f"  🧠 Calculando... Profundidad: {depth} | Reloj: {my_time_ms}", flush=True)

    best_move = None
    is_white = (board.turn == chess.WHITE)
    best_value = -math.inf if is_white else math.inf
    alpha = -math.inf
    beta = math.inf

    legal_moves = order_moves(board, list(board.legal_moves))

    for move in legal_moves:
        board.push(move)
        board_val = minimax(board, depth - 1, alpha, beta, not is_white)
        board.pop()

        if is_white:
            if board_val > best_value:
                best_value = board_val
                best_move = move
            alpha = max(alpha, board_val)
        else:
            if board_val < best_value:
                best_value = board_val
                best_move = move
            beta = min(beta, board_val)

    return best_move if best_move else legal_moves[0]

# ==========================================
# 3. CONEXIÓN A LICHESS Y DESAFÍOS AUTÓNOMOS
# ==========================================

TOKEN = os.environ.get("LICHESS_TOKEN")
if not TOKEN:
    raise ValueError("⚠️ LICHESS_TOKEN no está configurado.")

session = berserk.TokenSession(TOKEN)
client = berserk.Client(session=session)

try:
    my_profile = client.account.get()
    my_id = my_profile['id']
    my_username = my_profile['username']
except Exception as e:
    raise RuntimeError(f"⚠️ Error al conectar con Lichess: {e}")

is_in_game = False
is_rated_turn = True

def auto_challenge_loop():
    global is_in_game, is_rated_turn
    print("🤖 Gestor de Auto-Desafíos activado...", flush=True)
    time.sleep(10)
    
    while True:
        try:
            if not is_in_game:
                online_bots = list(client.bots.get_online_bots())
                available_bots = [b for b in online_bots if b.get('id') != my_id]

                if available_bots:
                    target = random.choice(available_bots)
                    target_id = target.get('id')
                    mode_str = "Clasificada" if is_rated_turn else "Amistosa"
                    
                    print(f"⚔️ Retando a {target_id} ({mode_str})...", flush=True)
                    try:
                        client.challenges.create(
                            username=target_id,
                            rated=is_rated_turn,
                            clock_limit=180,
                            clock_increment=2,
                            color='random'
                        )
                        is_rated_turn = not is_rated_turn
                    except Exception:
                        pass
                    
                    time.sleep(40)
                else:
                    time.sleep(15)
            else:
                time.sleep(10)
        except Exception:
            time.sleep(20)

threading.Thread(target=auto_challenge_loop, daemon=True).start()

# ==========================================
# 4. BUCLE PRINCIPAL (Streaming y Partidas)
# ==========================================

print(f"✅ Bot listo y conectado como: {my_username}", flush=True)

while True:
    try:
        for event in client.bots.stream_incoming_events():
            event_type = event.get('type')

            if event_type == 'challenge':
                challenge_id = event['challenge']['id']
                challenger_id = event['challenge'].get('challenger', {}).get('id')
                variant = event['challenge']['variant']['key']

                if challenger_id == my_id: continue

                if variant == 'standard':
                    try:
                        client.bots.accept_challenge(challenge_id)
                        print(f"🤝 Reto aceptado: {challenge_id}", flush=True)
                    except Exception: pass
                else:
                    try: client.bots.decline_challenge(challenge_id, reason='variant')
                    except Exception: pass

            elif event_type == 'gameStart':
                game_id = event['game']['gameId']
                is_in_game = True
                print(f"🎮 Partida iniciada: https://lichess.org/{game_id}", flush=True)
                
                # Limpiar caché al iniciar cada partida nueva
                transposition_table.clear()
                board = chess.Board()

                try:
                    game_active = True
                    while game_active:
                        try:
                            for game_event in client.bots.stream_game_state(game_id):
                                if game_event['type'] == 'gameFull':
                                    white_id = game_event['white'].get('id')
                                    is_white = (white_id == my_id)
                                    state = game_event['state']
                                elif game_event['type'] == 'gameState':
                                    state = game_event
                                else:
                                    continue

                                moves = state['moves'].split() if state['moves'] else []
                                board.reset()
                                for move in moves: board.push(chess.Move.from_uci(move))

                                if state['status'] != 'started' or board.is_game_over():
                                    print(f"🏁 Partida finalizada: {game_id}", flush=True)
                                    game_active = False
                                    break

                                is_my_turn = (board.turn == chess.WHITE and is_white) or (board.turn == chess.BLACK and not is_white)

                                if is_my_turn:
                                    start_time = time.time()
                                    
                                    # Extracción de tiempo segura contra timedelta o int
                                    raw_time = state.get('wtime', 180000) if is_white else state.get('btime', 180000)
                                    if isinstance(raw_time, datetime.timedelta):
                                        my_time_ms = raw_time.total_seconds() * 1000
                                    else:
                                        my_time_ms = int(raw_time) if raw_time else 180000
                                    
                                    best_move = get_best_move(board, my_time_ms=my_time_ms)
                                    elapsed = time.time() - start_time
                                    
                                    try:
                                        client.bots.make_move(game_id, best_move.uci())
                                        print(f"  👉 Jugada enviada: {best_move.uci()} (Cálculo: {elapsed:.2f}s)", flush=True)
                                    except Exception as e:
                                        print(f"⚠️ Error enviando mov: {e}", flush=True)
                        
                        except Exception as stream_err:
                            print(f"🔄 Reconectando stream de partida... ({stream_err})", flush=True)
                            time.sleep(1)
                            try:
                                current_game = client.games.export(game_id)
                                if current_game.get('status') != 'started':
                                    game_active = False
                            except Exception:
                                game_active = False
                finally:
                    is_in_game = False

    except Exception as err:
        print(f"🔌 Desconexión general de Lichess. Reconectando en 5s... ({err})", flush=True)
        time.sleep(5)
