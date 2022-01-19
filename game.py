import os
import time
from chessEnv import ChessEnv
from agent import Agent
import utils
import logging
import config
import chess
from chess.pgn import Game as ChessGame
from edge import Edge
from mcts import MCTS
import uuid
import pandas as pd
import numpy as np
import socket
import json

class Game:
    def __init__(self, env: ChessEnv, white: Agent, black: Agent):
        self.env = env
        self.white = white
        self.black = black

        self.memory = []

        self.reset()

    def reset(self):
        self.env.reset()
        self.turn = self.env.board.turn  # True = white, False = black

    @staticmethod
    def get_winner(result: str) -> int:
        return 1 if result == "1-0" else - 1 if result == "0-1" else 0


    @utils.time_function
    def play_one_game(self, stochastic: bool = True) -> int:
        # reset everything
        self.reset()
        # add a new memory entry
        self.memory.append([])
        # show the board
        logging.info(f"\n{self.env.board}")
        # counter to check amount of moves played. if above limit, estimate winner
        counter, previous_edges, full_game = 0, (None, None), True
        while not self.env.board.is_game_over():
            # play one move (previous move is used for updating the MCTS tree)
            previous_edges = self.play_move(stochastic=stochastic, previous_moves=previous_edges)
            # end if the game drags on too long
            counter += 1
            if counter > config.MAX_GAME_MOVES:
                # estimate the winner based on piece values
                winner = ChessEnv.estimate_winner(self.env.board)
                logging.info(f"Game over by move limit ({config.MAX_GAME_MOVES}). Result: {winner}")
                full_game = False
                break
        if full_game:
            # get the winner based on the result of the game
            winner = Game.get_winner(self.env.board.result())
            logging.info(f"Game over. Result: {winner}")
        # save game result to memory for all games
        for index, element in enumerate(self.memory[-1]):
            self.memory[-1][index] = (element[0], element[1], winner)

        game = ChessGame()
        # set starting position
        game.setup(self.env.fen)
        # add moves
        node = game.add_variation(self.env.board.move_stack[0])
        for move in self.env.board.move_stack[1:]:
            node = node.add_variation(move)
        # print pgn
        logging.info(game)

        # save memory to file
        self.save_game(name="game", full_game=full_game)

        return winner

    def play_move(self, stochastic: bool = True, previous_moves: tuple[Edge, Edge] = (None, None)) -> None:
        """
        Play one move. If stochastic is True, the move is chosen using a probability distribution.
        Otherwise, the move is chosen based on the highest N (deterministically).
        The previous moves are used to reuse the MCTS tree (if possible): the root node is set to the
        node found after playing the previous moves in the current tree.
        """
        # whose turn is it
        current_player = self.white if self.turn else self.black

        if previous_moves[0] is None or previous_moves[1] is None:
            # create new tree with root node == current board
            current_player.mcts = MCTS(current_player, state=self.env.board.fen())
        else:   
            # change the root node to the node after playing the two previous moves
            try:
                node = current_player.mcts.root.get_edge(previous_moves[0].action).output_node
                node = node.get_edge(previous_moves[1].action).output_node
                current_player.mcts.root = node
            except AttributeError:
                logging.warning("WARN: Node does not exist in tree, continuing with new tree...")
                current_player.mcts = MCTS(current_player, state=self.env.board.fen())
        # play n simulations from the root node
        current_player.run_simulations(n=config.SIMULATIONS_PER_MOVE)

        moves = current_player.mcts.root.edges

        # TODO: check if storing input state is faster/less space-consuming than storing the fen string
        self.save_to_memory(self.env.board.fen(), moves)

        sum_move_visits = sum(e.N for e in moves)
        probs = [e.N / sum_move_visits for e in moves]
        
        # added epsilon to avoid choosing random moves too many times
        if stochastic and np.random.random() < config.EPSILON:
            # choose a move based on a probability distribution
            best_move = np.random.choice(moves, p=probs)
        else:
            # choose a move based on the highest N
            best_move = moves[np.argmax(probs)]

        # play the move
        logging.info(
            f"{'White' if self.turn else 'Black'} played  {self.env.board.fullmove_number}. {best_move.action}")
        new_board = self.env.step(best_move.action)
        logging.info(f"\n{new_board}")
        logging.info(f"Value according to white: {self.white.mcts.root.value}")
        logging.info(f"Value according to black: {self.black.mcts.root.value}")

        # switch turn
        self.turn = not self.turn

        # return the previous move and the new move
        return (previous_moves[1], best_move)

    def save_to_memory(self, state, moves) -> None:
        sum_move_visits = sum(e.N for e in moves)
        # create dictionary of moves and their probabilities
        search_probabilities = {
            e.action.uci(): e.N / sum_move_visits for e in moves}
        # winner gets added after game is over
        self.memory[-1].append((state, search_probabilities, None))

    def save_game(self, name: str = "game", full_game: bool = False) -> None:
        # the game id consist of game + datetime
        game_id = f"{name}-{str(uuid.uuid4())[:8]}"
        if full_game:
            # if the game result was not estimated, save the game id to a seperate file (to look at later)
            with open("full_games.txt", "a") as f:
                f.write(f"{game_id}.npy\n")
        np.save(os.path.join(config.MEMORY_DIR, game_id), self.memory[-1])
        logging.info(
            f"Game saved to {os.path.join(config.MEMORY_DIR, game_id)}.npy")
        logging.info(f"Memory size: {len(self.memory)}")


    @utils.time_function
    def train_puzzles(self, puzzles: pd.DataFrame):
        """
        Create positions from puzzles (fen strings) and let the MCTS figure out how to solve them.
        The saved positions can be used to train the neural network.
        """
        logging.info(f"Training on {len(puzzles)} puzzles")
        for puzzle in puzzles.itertuples():
            self.env.fen = puzzle.fen
            self.env.reset()
            # play the first move
            moves = puzzle.moves.split(" ")
            self.env.board.push_uci(moves.pop(0))
            logging.info(f"Puzzle to solve ({puzzle.rating} ELO): {self.env.fen}")
            logging.info(f"\n{self.env.board}")
            logging.info(f"Correct solution: {moves} ({len(moves)} moves)")
            self.memory.append([])
            counter, previous_edges = 0, (None, None)
            while not self.env.board.is_game_over():
                # deterministically choose the next move (we want no exploration here)
                previous_edges = self.play_move(stochastic=False, previous_moves=previous_edges)
                counter += 1
                if counter > config.MAX_PUZZLE_MOVES:
                    logging.warning("Puzzle could not be solved within the move limit")
                    solved = False
                    break
            if not solved: 
                continue
            logging.info(f"Puzzle complete. Ended after {counter} moves: {self.env.board.result()}")
            # save game result to memory for all games
            winner = Game.get_winner(self.env.board.result())
            for index, element in enumerate(self.memory[-1]):
                self.memory[-1][index] = (element[0], element[1], winner)

            game = ChessGame()
            # set starting position
            game.setup(self.env.fen)
            # add moves
            node = game.add_variation(self.env.board.move_stack[0])
            for move in self.env.board.move_stack[1:]:
                logging.info(move)
                node = node.add_variation(move)
            # print pgn
            logging.info(game)

            # save memory to file
            self.save_game(name="puzzle")

    @staticmethod
    def create_puzzle_set(filename: str, type: str = "mateIn2") -> pd.DataFrame:
        start_time = time.time()
        puzzles: pd.DataFrame = pd.read_csv(filename, header=None)
        # drop unnecessary columns
        puzzles = puzzles.drop(columns=[0, 4, 5, 6, 8])
        # set column names
        puzzles.columns = ["fen", "moves", "rating", "type"]
        # only keep puzzles where type contains "mate"
        puzzles = puzzles[puzzles["type"].str.contains(type)]
        logging.info(f"Created puzzles in {time.time() - start_time} seconds")
        return puzzles
        

    def create_training_set(self):
        counter = {"white": 0, "black": 0, "draw": 0}
        while True:
            winner = self.play_one_game(stochastic=True)
            if winner == 1:
                counter["white"] += 1
            elif winner == -1:
                counter["black"] += 1
            else:
                counter["draw"] += 1
            logging.info(
                f"Game results: {counter['white']} - {counter['black']} - {counter['draw']}")
    
    @utils.time_function
    @staticmethod
    def test():
        # create socket client
        socket_to_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        socket_to_server.connect((config.SOCKET_HOST, config.SOCKET_PORT))
        logging.info(f"Connected to server {config.SOCKET_HOST}:{config.SOCKET_PORT}")

        board = chess.Board()

        for _ in range(1500):
            # create data
            data: np.ndarray = ChessEnv.state_to_input(board.fen())
            # send data
            socket_to_server.send(data)
            # receive data length
            data_length = socket_to_server.recv(10)
            data_length = int(data_length.decode('ascii'))
            # receive data
            data = utils.recvall(socket_to_server, data_length)
            # decode data
            data = data.decode('ascii')
            # json to dict
            data = json.loads(data)
            # make random move
            board.push(list(board.generate_legal_moves())[0])