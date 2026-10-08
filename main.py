import os
import time
import math
import random
import threading
from flask import Flask
import chess
import chess.polyglot
import berserk

# ==========================================
# 1. SERVIDOR HTTP (Keep-Alive para Render Free)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Lichess Bot Engine (>2000 ELO) en ejecución", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ==========================================
# 2. CONFIGURACIÓN DEL MOTOR Y EVALUACIÓN
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

# Piece-Square Tables (PST)
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

def evaluate_board(board: chess.Board) -> int:
    if board.is_checkmate():
        return -99999 if board.turn == chess.WHITE else 99999
    if board.is_stalemate() or board.is_insufficient_material() or board.is_threefold_repetition():
        return 0

    evaluation = 0
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is not None:
            val = PIECE_VALUES[piece.piece_type]
            sq = square if piece.color == chess.WHITE else chess.square_mirror(square)
            
            if piece.piece_type == chess.PAWN:
                val += pawntable[sq]
            elif piece.piece_type == chess.KNIGHT:
                val += knightstable[sq]
            elif piece.piece_type == chess.BISHOP:
                val += bishopstable[sq]
            elif piece.piece_type == chess.ROOK:
                val += rookstable[sq]
            elif piece.piece_type == chess.QUEEN:
                val += queenstable[sq]
            elif piece.piece_type == chess.KING:
                val += kingstable[sq]

            if piece.color == chess.WHITE:
                evaluation += val
            else:
                evaluation -= val

    return evaluation

def order_moves(board: chess.Board, moves):
    def score_move(move):
        score = 0
        if board.is_capture(move):
            attacker = board.piece_at(move.from_square)
            victim = board.piece_at(move.to_square)
            if attacker and victim:
                score += 10 * PIECE_VALUES[victim.piece_type] - PIECE_VALUES[attacker.piece_type]
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
    if depth == 0 or board.is_game_over():
        return quiescence_search(board, alpha, beta) if is_maximizing else -quiescence_search(board, -beta, -alpha)

    legal_moves = order_moves(board, list(board.legal_moves))

    if is_maximizing:
        max_eval = -math.inf
        for move in legal_moves:
            board.push(move)
            eval = minimax(board, depth - 1, alpha, beta, False)
            board.pop()
            max_eval = max(max_eval, eval)
            alpha = max(alpha, eval)
            if beta <= alpha:
                break
        return max_eval
    else:
        min_eval = math.inf
        for move in legal_moves:
            board.push(move)
            eval = minimax(board, depth - 1, alpha, beta, True)
            board.pop()
            min_eval = min(min_eval, eval)
            beta = min(beta, eval)
            if beta <= alpha:
                break
        return min_eval

def get_opening_move_polyglot(board: chess.Board) -> chess.Move | None:
    if not os.path.exists(BOOK_PATH):
        return None

    try:
        with chess.polyglot.open_reader(BOOK_PATH) as reader:
            entries = list(reader.find_all(board))
            if entries:
                entry = random.choice(entries)
                return entry.move
    except Exception as e:
        print(f"Error al leer book.bin: {e}")
    
    return None

def get_best_move(board: chess.Board, depth: int = 4) -> chess.Move:
    if board.fullmove_number <= 15:
        book_move = get_opening_move_polyglot(board)
        if book_move:
            print("  📖 Jugada ejecutada desde book.bin")
            return book_move

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
# 3. CONEXIÓN Y DESAFÍOS AUTÓNOMOS
# ==========================================

TOKEN = os.environ.get("LICHESS_TOKEN")
if not TOKEN:
    raise ValueError("LICHESS_TOKEN no configurado")

session = berserk.TokenSession(TOKEN)
client = berserk.Client(session=session)

my_profile = client.account.get()
my_id = my_profile['id']
my_username = my_profile['username']

is_in_game = False
is_rated_turn = True

def auto_challenge_loop():
    global is_in_game, is_rated_turn
    
    print("Iniciando gestor automático de desafíos a bots...")
    time.sleep(10)
    
    while True:
        try:
            if not is_in_game:
                online_bots = list(client.bots.get_online_bots())
                available_bots = [b for b in online_bots if b.get('id') != my_id]

                if available_bots:
                    target_bot = random.choice(available_bots)
                    target_id = target_bot.get('id')
                    
                    rated_mode = is_rated_turn
                    mode_str = "Clasificada (Rated)" if rated_mode else "Amistosa (Casual)"
                    
                    print(f"Enviando desafío {mode_str} al bot: {target_id}")
                    
                    try:
                        client.challenges.create(
                            username=target_id,
                            rated=rated_mode,
                            clock_limit=180,
                            clock_increment=2,
                            color='random',
                            variant='standard'
                        )
                        is_rated_turn = not is_rated_turn
                    except Exception as err:
                        print(f"Error al enviar reto a {target_id}: {err}")

                    time.sleep(40)
                else:
                    time.sleep(15)
            else:
                time.sleep(10)
        except Exception as e:
            print(f"Error en bucle de auto-desafío: {e}")
            time.sleep(20)

threading.Thread(target=auto_challenge_loop, daemon=True).start()

# ==========================================
# 4. BUCLE PRINCIPAL
# ==========================================

print(f"Bot listo y activo como: {my_username}")

for event in client.bots.stream_incoming_events():
    event_type = event.get('type')

    if event_type == 'challenge':
        challenge = event['challenge']
        challenge_id = challenge['id']
        variant = challenge['variant']['key']
        challenger_id = challenge.get('challenger', {}).get('id')

        # Ignorar retos creados por nosotros mismos
        if challenger_id == my_id:
            continue

        if variant == 'standard':
            try:
                client.bots.accept_challenge(challenge_id)
                print(f"Reto entrante aceptado: {challenge_id}")
            except berserk.exceptions.ResponseError as e:
                print(f"No se pudo aceptar el reto {challenge_id}: {e}")
        else:
            try:
                client.bots.decline_challenge(challenge_id, reason='variant')
            except berserk.exceptions.ResponseError:
                pass

    elif event_type == 'gameStart':
        game_id = event['game']['gameId']
        is_in_game = True
        print(f"Partida iniciada: {game_id}")
        
        board = chess.Board()

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
                for move in moves:
                    board.push(chess.Move.from_uci(move))

                if state['status'] != 'started' or board.is_game_over():
                    print(f"Partida finalizada: {game_id}")
                    break

                is_my_turn = (board.turn == chess.WHITE and is_white) or (board.turn == chess.BLACK and not is_white)

                if is_my_turn:
                    start_time = time.time()
                    depth = 4 if len(moves) < 30 else 3
                    
                    best_move = get_best_move(board, depth=depth)
                    elapsed = time.time() - start_time
                    
                    try:
                        client.bots.make_move(game_id, best_move.uci())
                        print(f"Jugada enviada [{game_id}]: {best_move.uci()} (en {elapsed:.2f}s)")
                    except berserk.exceptions.ResponseError as e:
                        print(f"Error enviando movimiento (partida probablemente terminada): {e}")
        finally:
            is_in_game = False
