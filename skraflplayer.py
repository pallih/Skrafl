# -*- coding: utf-8 -*-

""" Skraflplayer - an automatic SCRABBLE(tm) player

    Author: Vilhjalmur Thorsteinsson, 2014

    This module finds and ranks all legal moves on
    a SCRABBLE(tm)-like board.

    The algorithm is based on the classic paper by Appel & Jacobson,
    "The World's Fastest Scrabble Program",
    http://www.cs.cmu.edu/afs/cs/academic/class/15451-s06/www/lectures/scrabble.pdf

    The main class in the module is called AutoPlayer. Given a game State,
    it finds all legal moves, ranks them and returns the 'best'
    (currently the highest-scoring) move.

    Moves are found by examining each one-dimensional Axis of the board
    in turn, i.e. 15 rows and 15 columns for a total of 30 axes.
    For each Axis an array of Squares is constructed. The cross-check set
    of each empty Square is calculated, i.e. the set of letters that form
    valid words by connecting with word parts across the square's Axis.
    To save processing time, the cross-check sets are also intersected with
    the letters in the rack, unless the rack contains a blank tile.

    Any empty square with a non-null cross-check set or adjacent to
    a covered square within the axis is a potential anchor square.
    Each anchor square is examined in turn, from "left" to "right".
    The algorithm roughly proceeds as follows:

    1) Count the number of empty non-anchor squares to the left of
        the anchor. Call the number 'maxleft'.
    2) Generate all permutations of rack tiles found by navigating
        from the root of the DAWG, of length 1..maxleft, i.e. all
        possible word beginnings from the rack.
    3) For each such permutation, attempt to complete the
        word by placing the rest of the available tiles on the
        anchor square and to its right.
    4) In any case, even if maxleft=0, place a starting tile on the
        anchor square and attempt to complete a word to its right.
    5) When placing a tile on the anchor square or to its right,
        do so under three constraints: (a) the cross-check
        set of the square in question; (b) that there is
        a path in the DAWG corresponding to the tiles that have
        been laid down so far, incl. step 2 and 3; (c) a matching
        tile is still available in the rack (with blank tiles always
        matching).
    6) If extending to the right and coming to a tile that is
        already on the board, it must correspond to the DAWG path
        being followed.
    7) If we are running off the edge of the axis, or have come
        to an empty square, and we are at a final node in the
        DAWG indicating that a word is completed, we have a candidate
        move. Calculate its score and add it to the list of potential
        moves.

    Steps 1)-3) above are mostly implemented in the class LeftPartNavigator,
    while steps 4)-7) are found in ExtendRightNavigator. These classes
    correspond to the Appel & Jacobson LeftPart and ExtendRight functions.

    Note: SCRABBLE is a registered trademark. This software or its author
    are in no way affiliated with or endorsed by the owners or licensees
    of the SCRABBLE trademark.

"""

from dawgdictionary import DawgDictionary, Navigation
from skraflmechanics import Manager, State, Board, Cover, Move, ExchangeMove, PassMove
from languages import Alphabet


class Square:

    """ Represents a single square within an axis.
        A square knows about its cross-checks, i.e. which letters can be
        legally placed in the square while matching correctly with word
        parts above and/or below the square.
    """

    def __init__(self):
        # Cross checks, i.e. possible letters to be placed here,
        # represented as a bit pattern
        self._cc = 0
        # The tile located here, '?' if blank tile
        self._tile = None
        # The letter located here, including meaning of blank tile
        self._letter = None
        # Is this an anchor square?
        self._anchor = False

    def init(self, autoplayer, row, col, crosscheck):
        """ Initialize this square from the board """
        board = autoplayer.board()
        self._tile = board.tile_at(row, col)
        self._letter = board.letter_at(row, col)
        # Cross checks and anchors
        self._cc = crosscheck
        if self.is_open() and board.has_adjacent(row, col):
            # Empty square with adjacent covered squares and nonzero cross-checks:
            # mark as anchor
            self.mark_anchor()

    def is_empty(self):
        """ Is this square empty? """
        return self._letter == u' '

    def is_open(self):
        """ Can a new tile from the rack be placed here? """
        return self.is_empty() and bool(self._cc)

    def is_open_for(self, c):
        """ Can this letter be placed here? """
        return bool(self._cc & (1 << Alphabet.order.index(c)))

    def letter(self):
        """ Return the letter at this square """
        return self._letter

    def mark_anchor(self):
        """ Mark this square as an anchor """
        self._anchor = True

    def is_anchor(self):
        """ Is this an anchor square? """
        return self._anchor


class Axis:

    """ Represents a one-dimensional axis on the board, either
        horizontal or vertical. This is used to find legal moves
        for an AutoPlayer.
    """

    def __init__(self, autoplayer, index, horizontal):

        self._autoplayer = autoplayer
        self._sq = [None] * Board.SIZE
        for i in range(Board.SIZE):
            self._sq[i] = Square()
        self._index = index
        self._horizontal = horizontal
        self._rack = autoplayer.rack()
        # Bit pattern representing empty squares on this axis
        self._empty_bits = 0

    def is_horizontal(self):
        """ Is this a horizontal (row) axis? """
        return self._horizontal

    def is_vertical(self):
        """ Is this a vertical (column) axis? """
        return not self._horizontal

    def coordinate_of(self, index):
        """ Return the co-ordinate on the board of a square within this axis """
        return (self._index, index) if self._horizontal else (index, self._index)

    def coordinate_step(self):
        """ How to move along this axis on the board, (row,col) """
        return (0, 1) if self._horizontal else (1, 0)

    def letter_at(self, index):
        """ Return the letter at the index """
        return self._sq[index].letter()

    def is_open(self, index):
        """ Is the square at the index open (i.e. can a tile be placed there?) """
        return self._sq[index].is_open()

    def is_open_for(self, index, letter):
        """ Is the square at the index open for this letter? """
        return self._sq[index].is_open_for(letter)

    def is_empty(self, index):
        """ Is the square at the index empty? """
        return bool(self._empty_bits & (1 << index))

    def mark_anchor(self, index):
        """ Force the indicated square to be an anchor. Used in first move
            to mark the center square. """
        self._sq[index].mark_anchor()

    def init_crosschecks(self):
        """ Calculate and return a list of cross-check bit patterns for the indicated axis """

        # The cross-check set is the set of letters that can appear in a square
        # and make cross words (above/left and/or below/right of the square) valid
        board = self._autoplayer.board()
        # Prepare to visit all squares on the axis
        x, y = self.coordinate_of(0)
        xd, yd = self.coordinate_step()
        # Fetch the default cross-check bits, which depend on the rack.
        # If the rack contains a wildcard (blank tile), the default cc set
        # contains all letters in the Alphabet. Otherwise, it contains the
        # letters in the rack.
        all_cc = self._autoplayer.rack_bit_pattern()
        # Go through the open squares and calculate their cross-checks
        for ix in range(Board.SIZE):
            cc = all_cc # Start with the default cross-check set
            if not board.is_covered(x, y):
                if self.is_horizontal():
                    above = board.letters_above(x, y)
                    below = board.letters_below(x, y)
                else:
                    above = board.letters_left(x, y)
                    below = board.letters_right(x, y)
                query = u'' if not above else above
                query += u'?'
                if below:
                    query += below
                if len(query) > 1:
                    # Nontrivial cross-check: Query the word database for words that fit this pattern
                    matches = Manager.word_db().find_matches(query, False) # Don't need a sorted result
                    bits = 0
                    if matches:
                        cix = 0 if not above else len(above)
                        # Note the set of allowed letters here
                        bits = Alphabet.bit_pattern([wrd[cix] for wrd in matches])
                    # Reduce the cross-check set by intersecting it with the allowed set.
                    # If the cross-check set and the rack have nothing in common, this
                    # will lead to the square being marked as closed, which saves
                    # calculation later on
                    cc &= bits
            # Initialize the square
            self._sq[ix].init(self._autoplayer, x, y, cc)
            # Keep track of empty squares within the axis in a bit pattern for speed
            if self._sq[ix].is_empty():
                self._empty_bits |= (1 << ix)
            x += xd
            y += yd

    def _gen_moves_from_anchor(self, index, maxleft, lpn):
        """ Find valid moves emanating (on the left and right) from this anchor """

        if maxleft == 0 and index > 0 and not self.is_empty(index - 1):
            # We have a left part already on the board: try to complete it
            leftpart = u''
            ix = index
            while ix > 0 and not self.is_empty(ix - 1):
                leftpart = self._sq[ix - 1]._letter + leftpart
                ix -= 1
            # Use the ExtendRightNavigator to find valid words with this left part
            nav = LeftFindNavigator(leftpart)
            Manager.word_db().navigate(nav)
            ns = nav.state()
            if ns is not None:
                # We found a matching prefix in the graph
                matched, prefix, nextnode = ns
                assert matched == leftpart
                nav = ExtendRightNavigator(self, index, self._rack)
                Navigation(nav).resume(prefix, nextnode, leftpart)
            return

        # We are not completing an existing left part
        # Begin by extending an empty prefix to the right, i.e. placing
        # tiles on the anchor square itself and to its right
        nav = ExtendRightNavigator(self, index, self._rack)
        Manager.word_db().navigate(nav)

        if maxleft > 0 and lpn is not None:
            # Follow this by an effort to permute left prefixes into the open space
            # to the left of the anchor square
            for leftlen in range(1, maxleft + 1):
                lplist = lpn.leftparts(leftlen)
                if lplist is not None:
                    for leftpart, rackleave, prefix, nextnode in lplist:
                        nav = ExtendRightNavigator(self, index, rackleave)
                        Navigation(nav).resume(prefix, nextnode, leftpart)

    def generate_moves(self, lpn):
        """ Find all valid moves on this axis by attempting to place tiles
            at and around all anchor squares """
        last_anchor = -1
        lenrack = len(self._rack)
        for i in range(Board.SIZE):
            if self._sq[i].is_anchor():
                # Count the consecutive open, non-anchor squares on the left of the anchor
                opensq = 0
                left = i
                while left > 0 and left > (last_anchor + 1) and self._sq[left - 1].is_open():
                    opensq += 1
                    left -= 1
                # We have a maximum left part length of min(opensq, lenrack-1) as the anchor
                # square itself must always be filled from the rack
                self._gen_moves_from_anchor(i, min(opensq, lenrack - 1), lpn)
                last_anchor = i

    
class LeftPermutationNavigator:

    """ A navigation class to be used with DawgDictionary.navigate()
        to find all left parts of words that are possible with
        a particular rack. The results are accumulated by length.
        This calculation is only done once at the start of move
        generation for a particular rack and board.
    """

    def __init__(self, rack):
        self._rack = rack
        self._stack = []
        self._maxleft = len(rack) - 1 # One tile on the anchor itself
        assert self._maxleft > 0
        self._leftparts = [None] * self._maxleft
        self._index = 0

    def leftparts(self, length):
        """ Returns a list of leftparts of the length requested """
        return self._leftparts[length - 1] if length > 0 and length <= self._maxleft else None

    def push_edge(self, firstchar):
        """ Returns True if the edge should be entered or False if not """
        # Follow all edges that match a letter in the rack
        # (which can be '?', matching all edges)
        if not ((firstchar in self._rack) or (u'?' in self._rack)):
            return False
        # Fit: save our rack and move into the edge
        self._stack.append((self._rack, self._index))
        return True

    def accepting(self):
        """ Returns False if the navigator does not want more characters """
        # Continue until we have generated all left parts possible from the
        # rack but leaving at least one tile
        return self._index < self._maxleft

    def accepts(self, newchar):
        """ Returns True if the navigator will accept the new character """
        exactmatch = newchar in self._rack
        if (not exactmatch) and (u'?' not in self._rack):
            # Can't continue with this prefix - we no longer have rack letters matching it
            return False
        # We're fine with this: accept the character and remove from the rack
        self._index += 1
        if exactmatch:
            self._rack = self._rack.replace(newchar, u'', 1)
        else:
            self._rack = self._rack.replace(u'?', u'', 1)
        return True

    def accept_resumable(self, prefix, nextnode, matched):
        """ Called to inform the navigator of a match and whether it is a final word """
        # Accumulate all possible left parts, by length
        lm = len(matched) - 1
        if self._leftparts[lm] is None:
            self._leftparts[lm] = list()
        # Store the matched word part as well as the remaining part
        # of the prefix of the edge we were on, and the next node.
        # This gives us the ability to resume the navigation later at
        # the saved point, to generate right parts.
        self._leftparts[lm].append((matched, self._rack, prefix, nextnode))

    def pop_edge(self):
        """ Called when leaving an edge that has been navigated """
        self._rack, self._index = self._stack.pop()
        # We need to visit all outgoing edges, so return True
        return True

    def done(self):
        """ Called when the whole navigation is done """
        pass


class LeftFindNavigator:

    """ A navigation class to trace a left part that is
        already on the board, and note its ending position in
        the graph.
    """

    def __init__(self, prefix):
        # The prefix to the left of the anchor
        self._prefix = prefix
        self._lenp = len(prefix)
        # Prefix index
        self._pix = 0
        self._state = None

    def state(self):
        return self._state

    def push_edge(self, firstchar):
        """ Returns True if the edge should be entered or False if not """
        # If we are still navigating through the prefix, do a simple compare
        return firstchar == self._prefix[self._pix]

    def accepting(self):
        """ Returns False if the navigator does not want more characters """
        return self._pix < self._lenp

    def accepts(self, newchar):
        """ Returns True if the navigator will accept the new character """
        if self._prefix[self._pix] != newchar:
            assert False
            return False # Should not happen - all prefixes should exist in the graph
        # So far, so good: move on
        self._pix += 1
        return True

    def accept_resumable(self, prefix, nextnode, matched):
        """ Called to inform the navigator of a match and whether it is a final word """
        if self._pix == self._lenp:
            # Found the left part: save the position (state)
            self._state = (matched, prefix, nextnode)

    def pop_edge(self):
        """ Called when leaving an edge that has been navigated """
        return False

    def done(self):
        """ Called when the whole navigation is done """
        pass


class Match:

    """ Return codes for the _check() function in ExtendRightNavigator """

    NO = 0
    BOARD_TILE = 1
    RACK_TILE = 2


class ExtendRightNavigator:

    """ A navigation class to be used with DawgDictionary.navigate()
        to perform the Appel & Jacobson ExtendRight function. This
        places rack tiles on and to the right of an anchor square, in
        conformance with the cross-checks and the tiles already on
        the board.
    """

    def __init__(self, axis, anchor, rack):
        self._axis = axis
        self._rack = rack
        self._anchor = anchor
        # The tile we are placing next
        self._index = anchor
        self._stack = []
        # Cache the initial check we do when pushing into an edge
        self._last_check = None

    def _check(self, ch):
        """ Check whether the letter ch could be placed at the
            current square, given the cross-checks and the rack """
        if not self._axis.is_empty(self._index):
            # There is a tile already in the square: we must match it exactly
            return Match.BOARD_TILE if (ch == self._axis.letter_at(self._index)) else Match.NO
        # Open square: apply cross-check constraints to the rack
        # Would this character pass the cross-checks?
        if not self._axis.is_open_for(self._index, ch):
            return Match.NO
        if u'?' in self._rack:
            # We could always use the wildcard in the rack to cover this, so OK
            return Match.RACK_TILE
        # Filter the rack by the applicable cross checks, and see whether
        # the candidate edge prefix matches that
        return Match.RACK_TILE if ch in self._rack else Match.NO

    def push_edge(self, firstchar):
        """ Returns True if the edge should be entered or False if not """
        # We are in the right part: check whether we have a potential match
        self._last_check = self._check(firstchar)
        if self._last_check == Match.NO:
            return False
        # Match: save our rack and our index and move into the edge
        self._stack.append((self._rack, self._index))
        return True

    def accepting(self):
        """ Returns False if the navigator does not want more characters """
        # Continue as long as there is something left to check
        if self._index >= Board.SIZE:
            # Gone off the board edge
            return False
        # Otherwise, continue while we have something on the rack
        # or we're at an occupied square
        return self._rack or (not self._axis.is_empty(self._index))

    def accepts(self, newchar):
        """ Returns True if the navigator will accept the new character """
        # We are on the anchor square or to its right
        # Use the cached check from push_edge if we have one
        match = self._check(newchar) if self._last_check is None else self._last_check
        self._last_check = None
        if match == Match.NO:
            # Something doesn't fit anymore, so we're done with this edge
            return False
        # We're fine with this: accept the character and remove from the rack
        self._index += 1
        if match == Match.RACK_TILE:
            # We used a rack tile: remove it from the rack before continuing
            if newchar in self._rack:
                self._rack = self._rack.replace(newchar, u'', 1)
            else:
                # Must be wildcard: remove it
                assert u'?' in self._rack
                self._rack = self._rack.replace(u'?', u'', 1)
        return True

    def accept(self, matched, final):
        """ Called to inform the navigator of a match and whether it is a final word """
        if final and len(matched) > 1 and (self._index >= Board.SIZE or
            self._axis.is_empty(self._index)):

            # Solution found - make a Move object for it and add it to the AutoPlayer's list
            ix = self._index - len(matched) # The word's starting index within the axis
            row, col = self._axis.coordinate_of(ix)
            xd, yd = self._axis.coordinate_step()
            move = Move(matched, row, col, self._axis.is_horizontal())
            # Fetch the rack as it was at the beginning of move generation
            autoplayer = self._axis._autoplayer
            rack = autoplayer.rack()
            tiles = u''
            for c in matched:
                if self._axis.is_empty(ix):
                    # Empty square that is being covered by this move
                    # Find out whether it is a blank or normal letter tile
                    if c in rack:
                        rack = rack.replace(c, u'', 1)
                        tile = c
                        tiles += c
                    else:
                        # Must be a wildcard match
                        rack = rack.replace(u'?', u'', 1)
                        tile = u'?'
                        tiles += tile + c
                    assert row in range(Board.SIZE)
                    assert col in range(Board.SIZE)
                    # Add this cover to the Move object
                    move.add_validated_cover(Cover(row, col, tile, c))
                else:
                    tiles += c
                ix += 1
                row += xd
                col += yd
            # Note the tiles played in the move
            move.set_tiles(tiles)
            # Check that we've picked off the correct number of tiles
            assert len(rack) == len(self._rack)
            autoplayer.add_candidate(move)

    def pop_edge(self):
        """ Called when leaving an edge that has been navigated """
        self._rack, self._index = self._stack.pop()
        # Once past the prefix, we need to visit all outgoing edges, so return True
        return True

    def done(self):
        """ Called when the whole navigation is done """
        pass


class AutoPlayer:

    """ Implements an automatic, computer-controlled player.
        All legal moves on the board are generated and the
        best move is then selected within the _find_best_move()
        function. This base class has a simple implementation
        of _find_best_move() that always chooses the best-scoring
        move. Other derived classes, such as AutoPlayer_MinMax,
        use more sophisticated heuristics to choose a move.
    """

    def __init__(self, state):

        # List of valid, candidate moves
        self._candidates = []
        self._state = state
        self._board = state.board()
        # The rack that the autoplayer has to work with
        self._rack = state.player_rack().contents()

        # Calculate a bit pattern representation of the rack
        if u'?' in self._rack:
            # Wildcard in rack: all letters allowed
            self._rack_bit_pattern = Alphabet.all_bits_set()
        else:
            # No wildcard: limits the possibilities of covering squares
            self._rack_bit_pattern = Alphabet.bit_pattern(self._rack)

    def board(self):
        """ Return the board """
        return self._board

    def rack(self):
        """ Return the rack, as a string of tiles """
        return self._rack

    def rack_bit_pattern(self):
        """ Return the bit pattern corresponding to the rack """
        return self._rack_bit_pattern

    def candidates(self):
        """ The list of valid, candidate moves """
        return self._candidates

    def add_candidate(self, move):
        """ Add a candidate move to the AutoPlayer's list """
        self._candidates.append(move)

    def _axis_from_row(self, row):
        """ Create and initialize an Axis from a board row """
        return Axis(self, row, True) # Horizontal

    def _axis_from_column(self, col):
        """ Create and initialize an Axis from a board column """
        return Axis(self, col, False) # Vertical

    def generate_move(self):
        """ Finds and returns a Move object to be played """
        return self._generate_move(depth = 1)

    def _generate_move(self, depth):
        """ Finds and returrns a Move object to be played, weighted by countermoves """

        # Start by generating all possible permutations of the
        # rack that form left parts of words, ordering them by length
        if len(self._rack) > 1:
            lpn = LeftPermutationNavigator(self._rack)
            Manager.word_db().navigate(lpn)
        else:
            lpn = None

        # Generate moves in one-dimensional space by looking at each axis
        # (row or column) on the board separately

        if self._board.is_empty():
            # Special case for first move: only consider the vertical
            # central axis (any move played there can identically be
            # played horizontally), and with only one anchor in the
            # middle square
            axis = self._axis_from_column(Board.SIZE // 2)
            axis.init_crosschecks()
            # Mark the center anchor
            axis.mark_anchor(Board.SIZE // 2)
            axis.generate_moves(lpn)
        else:
            # Normal move: go through all 15 (row) + 15 (column) axes and generate
            # valid moves within each of them
            for r in range(Board.SIZE):
                axis = self._axis_from_row(r)
                axis.init_crosschecks()
                axis.generate_moves(lpn)
            for c in range(Board.SIZE):
                axis = self._axis_from_column(c)
                axis.init_crosschecks()
                axis.generate_moves(lpn)
        # Delete the reference to LeftPermutationNavigator to save memory
        lpn = None
        # We now have a list of valid candidate moves; pick the best one
        move = self._find_best_move(depth)
        if move is not None:
            return move
        # Can't do anything: try exchanging all tiles
        if self._state.is_exchange_allowed():
            return ExchangeMove(self.rack())
        # If we can't exchange tiles, we have to pass
        return PassMove()

    def _find_best_move(self, depth):
        """ Analyze the list of candidate moves and pick the highest-scoring one """

        assert depth >= 0

        if not self._candidates:
            # No moves: must exchange or pass instead
            return None

        if len(self._candidates) == 1:
            # Only one legal move: play it
            return self._candidates[0]

        # Calculate the score of each candidate
        scored_candidates = [(m, m.score(self._board)) for m in self._candidates]

        def keyfunc(x):
            # Sort moves first by descending score;
            # in case of ties prefer shorter words
            # !!! TODO: Insert more sophisticated logic here,
            # including whether triple-word-score opportunities
            # are being opened for the opponent, minimal use
            # of blank tiles, leaving a good vowel/consonant
            # balance on the rack, etc.
            return (- x[1], x[0].num_covers())

        def keyfunc_firstmove(x):
            # Special case for first move:
            # Sort moves first by descending score, and in case of ties,
            # try to go to the upper half of the board for a more open game
            return (- x[1], x[0]._row)

        # Sort the candidate moves using the appropriate key function
        if self._board.is_empty():
            # First move
            scored_candidates.sort(key=keyfunc_firstmove)
        else:
            # Subsequent moves
            scored_candidates.sort(key=keyfunc)

        # Simply return the top scoring move
        # print(u"Autoplayer: Rack '{0}' generated {1} candidate moves:".format(self._rack, len(scored_candidates)))
        # Show top 20 candidates
        # for m, sc in scored_candidates[0:20]:
        #    print(u"Move {0} score {1}".format(m, sc))
        return scored_candidates[0][0]


class AutoPlayer_MiniMax(AutoPlayer):

    """ This subclass of AutoPlayer uses a MiniMax algorithm to
        select a move to play from the list of valid moves.
    """

    def __init__(self, state):
        AutoPlayer.__init__(self, state)

    def _find_best_move(self, depth):
        """ Analyze the list of candidate moves and pick the best one """

        assert depth >= 0

        if not self._candidates:
            # No moves: must exchange or pass instead
            return None

        if len(self._candidates) == 1:
            # Only one legal move: play it
            return self._candidates[0]

        # !!! TODO: Consider looking at exchange moves if there are
        # few and weak candidates

        # Calculate the score of each candidate
        scored_candidates = [(m, m.score(self._board)) for m in self._candidates]

        def keyfunc(x):
            # Sort moves first by descending score;
            # in case of ties prefer shorter words
            # !!! TODO: Insert more sophisticated logic here,
            # including whether triple-word-score opportunities
            # are being opened for the opponent, minimal use
            # of blank tiles, leaving a good vowel/consonant
            # balance on the rack, etc.
            return (- x[1], x[0].num_covers())

        def keyfunc_firstmove(x):
            # Special case for first move:
            # Sort moves first by descending score, and in case of ties,
            # try to go to the upper half of the board for a more open game
            return (- x[1], x[0]._row)

        # Sort the candidate moves using the appropriate key function
        if self._board.is_empty():
            # First move
            scored_candidates.sort(key=keyfunc_firstmove)
        else:
            # Subsequent moves
            scored_candidates.sort(key=keyfunc)

        # If we're not going deeper into the minimax analysis,
        # cut the crap and simply return the top scoring move
        if depth == 0:
            return scored_candidates[0][0]

        # Weigh top candidates by alpha-beta testing of potential
        # moves and counter-moves

        # !!! TODO: In endgame, if we have moves that complete the game (use all rack tiles)
        # we need not consider opponent countermoves

        NUM_TEST_RACKS = 20 # How many random test racks to try for statistical average
        NUM_CANDIDATES = 12 # How many top candidates do we look at with MiniMax?

        weighted_candidates = []
        min_score = None

        print(u"Looking at {0} top scoring candidate moves".format(NUM_CANDIDATES))
        # Look at the top scoring candidates
        for m, score in scored_candidates[0:NUM_CANDIDATES]:

            print(u"Candidate move {0} with raw score {1}".format(m, score))

            # Create a game state where the candidate move has been played
            teststate = State(copy = self._state) # Copy constructor
            teststate.apply_move(m)

            countermoves = list()

            if teststate.is_game_over():
                # This move finishes the game. The opponent then scores nothing
                # !!! TODO: (and in fact we get her tile score, but leave that aside here)
                avg_score = 0.0
                countermoves.append(0)
            else:
                # Loop over NUM_TEST_RACKS random racks to find the average countermove score
                sum_score = 0
                rackscores = dict()
                for _ in range(NUM_TEST_RACKS):
                    # Make sure we test this for a random opponent rack
                    teststate.randomize_and_sort_rack()
                    rack = teststate.player_rack().contents()
                    if rack in rackscores:
                        # We have seen this rack before: fetch its score
                        sc = rackscores[rack]
                    else:
                        # New rack: see how well it would score
                        apl = AutoPlayer_MiniMax(teststate)
                        # Go one level deeper into move generation
                        move = apl._generate_move(depth = depth - 1)
                        # Calculate the score of this random rack based move
                        # but do not apply it to the teststate
                        sc = teststate.score(move)
                        if sc > 100:
                            print(u"Countermove rack '{0}' generated move {1} scoring {2}".format(rack, move, sc))
                        # Cache the score
                        rackscores[rack] = sc
                    sum_score += sc
                    countermoves.append(sc)
                # Calculate the average score of the countermoves to this candidate
                # !!! TODO: Maybe a median score is better than average?
                avg_score = float(sum_score) / NUM_TEST_RACKS

            print(u"Average score of {0} countermove racks is {1:.2f}".format(NUM_TEST_RACKS, avg_score))
            print(countermoves)

            # Keep track of the lowest countermove score across all candidates as a baseline
            min_score = avg_score if (min_score is None) or (avg_score < min_score) else min_score
            # Keep track of the weighted candidate moves
            weighted_candidates.append((m, score, avg_score))

        print(u"Lowest score of countermove to all evaluated candidates is {0:.2f}".format(min_score))
        # Sort the candidates by the plain score after subtracting the effect of
        # potential countermoves, measured as the countermove score in excess of
        # the lowest countermove score found
        weighted_candidates.sort(key = lambda x: float(x[1]) - (x[2] - min_score), reverse = True)

        print(u"AutoPlayer_MinMax: Rack '{0}' generated {1} candidate moves:".format(self._rack, len(scored_candidates)))
        # Show top 20 candidates
        for m, sc, wsc in weighted_candidates:
            print(u"Move {0} score {1} weighted {2:.2f}".format(m, sc, float(sc) - (wsc - min_score)))
        # Return the highest-scoring candidate
        return weighted_candidates[0][0]

