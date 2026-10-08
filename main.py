import os
import random
import threading
from flask import Flask
import chess
import berserk

# --- Mini Servidor HTTP para Render Free ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Lichess Bot está vivo y ejecutándose", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Iniciar servidor Flask en un hilo secundario
threading.Thread(target=run_flask, daemon=True).start()

# --- Código del Bot de Lichess ---
TOKEN = os.environ.get("LICHESS_TOKEN")

if not TOKEN:
    raise ValueError("No se encontró la variable de entorno LICHESS_TOKEN")

session = berserk.TokenSession(TOKEN)
client = berserk.Client(session=session)

my_profile = client.account.get()
my_id = my_profile['id']
print(f"Bot conectado como: {my_profile['username']}")

for event in client.bots.stream_incoming_events():
    event_type = event.get('type')

    if event_type == 'challenge':
        challenge = event['challenge']
        challenge_id = challenge['id']
        variant = challenge['variant']['key']

        if variant == 'standard':
            client.bots.accept_challenge(challenge_id)
            print(f"Reto aceptado: {challenge_id}")
        else:
            client.bots.decline_challenge(challenge_id, reason='variant')

    elif event_type == 'gameStart':
        game_id = event['game']['gameId']
        print(f"Nueva partida iniciada: {game_id}")
        
        board = chess.Board()

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
                legal_moves = list(board.legal_moves)
                if legal_moves:
                    chosen_move = random.choice(legal_moves)
                    client.bots.make_move(game_id, chosen_move.uci())
                    print(f"Jugada enviada [{game_id}]: {chosen_move.uci()}")
