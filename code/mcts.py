# implement the Monte Carlo Tree Search algorithm
import chess
import chess.pgn
from chessEnv import ChessEnv
from node import Node
from edge import Edge
import numpy as np
import time
from tqdm import tqdm
import utils
import threading
# import tensorflow as tf

# graphing mcts
from graphviz import Digraph

import config
# output vector mapping
from mapper import Mapping

import logging


class MCTS:
    def __init__(self, agent: "Agent", state: str = chess.STARTING_FEN, stochastic=False):
        """
        An object of the MCTS class represents a tree that can be built using 
        the Monte Carlo Tree Search algorithm. The tree contists of nodes and edges.
        The root node represents the current move of the game.

        Hundreds of simulations are run to build the tree.
        """
        self.root = Node(state=state)

        self.game_path: list[Edge] = []
        self.cur_board: chess.Board = None

        self.agent = agent
        self.stochastic = stochastic

    def run_simulations(self, n: int) -> None:
        """
        Run n simulations from the root node.
        1) select child
        2) expand and evaluate
        3) backpropagate
        """
        for _ in tqdm(range(n)):
            self.game_path = []

            # traverse the tree by selecting edges with max Q+U
            # leaf is root on first iteration
            leaf = self.select_child(self.root)

            # expand the leaf node
            leaf.N += 1
            leaf = self.expand(leaf)

            # backpropagate the result
            leaf = self.backpropagate(leaf, leaf.value)

    def select_child(self, node: Node) -> Node:
        """
        Traverse the three from the given node, by selecting actions with the maximum Q+U.

        If the node has not been visited yet, return the node. That is the new leaf node.
        If this is the first simulation, the leaf node is the root node.
        """
        # traverse the tree by selecting nodes until a leaf node is reached
        while not node.is_leaf():
            if not len(node.edges):
                # if the node is terminal, return the node
                return node
            noise = [1 for _ in range(len(node.edges))]
            if self.stochastic and node == self.root:
                noise = np.random.dirichlet([config.DIRICHLET_NOISE]*len(node.edges))
            best_edge = None
            best_score = -np.inf                
            for i, edge in enumerate(node.edges):
                if edge.upper_confidence_bound(noise[i]) > best_score:
                    best_score = edge.upper_confidence_bound(noise[i])
                    best_edge = edge

            if best_edge is None:
                # this should never happen
                raise Exception("No edge found")
        
            # get that actions's new node
            node = best_edge.output_node
            self.game_path.append(best_edge)
        return node

    def map_valid_move(self, move: chess.Move) -> None:
        """
        Input: a valid move generated by the chess library.
        Will add the move to the output vector, along with its plane, column, and row
        """
        logging.debug("Filtering valid moves...")
        from_square = move.from_square
        to_square = move.to_square

        plane_index: int = None
        piece = self.cur_board.piece_at(from_square)
        direction = None

        if piece is None:
            raise Exception(f"No piece at {from_square}")

        if move.promotion and move.promotion != chess.QUEEN:
            piece_type, direction = Mapping.get_underpromotion_move(
                move.promotion, from_square, to_square)
            plane_index = Mapping.mapper[piece_type][1 - direction]
        else:
            # find the correct plane based on from_square and move_square
            if piece.piece_type == chess.KNIGHT:
                # get direction
                direction = Mapping.get_knight_move(from_square, to_square)
                plane_index = Mapping.mapper[direction]
            else:
                # get direction of queen-type move
                direction, distance = Mapping.get_queenlike_move(
                    from_square, to_square)
                plane_index = Mapping.mapper[direction][np.abs(distance)-1]
        # create a mask with only valid moves
        row = from_square % 8
        col = 7 - (from_square // 8)
        self.outputs.append((move, plane_index, row, col))

    def probabilities_to_actions(self, probabilities: list, board: str) -> dict:
        """
        Map the output vector of 4672 probabilities to moves. Returns a dictionary of moves and their probabilities.

        The output vector is a list of probabilities for every move
        * 4672 probabilities = 73*64 => 73 planes of 8x8

        The squares in these 8x8 planes indicate the square where the piece is.

        The plane itself indicates the type of move:
            - first 56 planes: queen moves (length of 7 squares * 8 directions)
            - next 8 planes: knight moves (8 directions)
            - final 9 planes: underpromotions (left diagonal, right diagonal, forward) * (three possible pieces (knight, bishop, rook))
        """
        probabilities = probabilities.reshape(
            config.amount_of_planes, config.n, config.n)
        # mask = np.zeros((config.amount_of_planes, config.n, config.n))

        actions = {}

        # only get valid moves
        self.cur_board = chess.Board(board)
        valid_moves = self.cur_board.generate_legal_moves()
        self.outputs = []
        # use threading to map valid moves quicker
        threads = []
        while True:
            try:
                move = next(valid_moves)
            except StopIteration:
                break
            thread = threading.Thread(
                target=self.map_valid_move, args=(move,))
            threads.append(thread)
            thread.start()

        # wait until all threads are done
        for thread in threads:
            thread.join()

        for move, plane_index, col, row in self.outputs:
            # mask[plane_index][col][row] = 1
            actions[move.uci()] = probabilities[plane_index][col][row]

        # utils.save_output_state_to_imgs(mask, "tests/output_planes", "mask")
        # utils.save_output_state_to_imgs(probabilities, "tests/output_planes", "unfiltered")

        # use the mask to filter the probabilities
        # probabilities = np.multiply(probabilities, mask)

        # utils.save_output_state_to_imgs(probabilities, "tests/output_planes", "filtered")
        return actions

    def expand(self, leaf: Node) -> Node:
        """
        Expand the leaf node by adding all possible moves to the leaf node.
        This will generate new edges and nodes.
        Return the leaf node
        """
        logging.debug("Expanding...")

        board = chess.Board(leaf.state)

        # get all possible moves
        possible_actions = list(board.generate_legal_moves())

        if not len(possible_actions):
            assert board.is_game_over(), "Game is not over, but there are no possible moves?"
            outcome = board.outcome(claim_draw=True)
            if outcome is None:
                leaf.value = 0
            else:
                leaf.value = 1 if outcome.winner == chess.WHITE else -1
            # print(f"Leaf's game ended with {leaf.value}")
            return leaf

        # predict p and v
        # p = array of probabilities: [0, 1] for every move (including invalid moves)
        # v = [-1, 1]
        input_state = ChessEnv.state_to_input(leaf.state)
        p, v = self.agent.predict(input_state)

        # map probabilities to moves, this also filters out invalid moves
        # returns a dictionary of moves and their probabilities
        # p, v = p[0], v[0][0]
        actions = self.probabilities_to_actions(p, leaf.state)

        logging.debug(f"Model predictions: {p}")
        logging.debug(f"Value of state: {v}")

        leaf.value = v

        # create a child node for every action
        for action in possible_actions:
            # make the move and get the new board
            new_state = leaf.step(action)
            # add a new child node with the new board, the action taken and its prior probability
            leaf.add_child(Node(new_state), action, actions[action.uci()])
        return leaf

    def backpropagate(self, end_node: Node, value: float) -> Node:
        """
        The backpropagation step will update the values of the nodes 
        in the traversed path from the given leaf node up to the root node.
        """
        logging.debug("Backpropagation...")

        for edge in self.game_path:
            edge.input_node.N += 1
            edge.N += 1
            edge.W += value
        return end_node

    def plot_node(self, dot: Digraph, node: Node):
        """
        Recursive function to plot nodes.
        """
        dot.node(f"{node.state}", f"N")
        for edge in node.edges:
            dot.edge(str(edge.input_node.state), str(
                edge.output_node.state), label=edge.action.uci())
            dot = self.plot_node(dot, edge.output_node)
        return dot

    def plot_tree(self, save_path: str = "tests/mcts_tree.gv") -> None:
        """
        Plot the MCTS tree using graphviz.
        """
        logging.debug("Plotting tree...")
        # tree plotting
        dot = Digraph(comment='Chess MCTS Tree')
        logging.info(f"# of nodes in tree: {len(self.root.get_all_children())}")

        # recursively plot the tree
        dot = self.plot_node(dot, self.root)
        dot.save(save_path)
