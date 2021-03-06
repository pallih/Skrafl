# -*- coding: utf-8 -*-

""" DAWG dictionary builder

    Author: Vilhjalmur Thorsteinsson, 2014

    DawgBuilder uses a Directed Acyclic Word Graph (DAWG)
    to store a large set of words in an efficient structure in terms
    of storage and speed.

    The DAWG implementation is partially based on Steve Hanov's work
    (see http://stevehanov.ca/blog/index.php?id=115), which references
    a paper by Daciuk et al (http://www.aclweb.org/anthology/J00-1002.pdf).

    This implementation compresses node sequences with single edges between
    them into single multi-letter edges. It also removes redundant edges
    to "pure" final nodes.

    DawgBuilder reads a set of text input files containing plain words,
    one word per line, and outputs a text file with a compressed
    graph. This file is read by the DawgDictionary class; see
    dawgdictionary.py

    The output file is structured as a sequence of lines. Each line
    represents a node in the graph and contains information about
    outgoing edges from the node. Nodes are referred to by their
    line number, where the starting root node is in line 1 and subsequent
    nodes are numbered starting with 2.

    A node (line) is represented as follows:

    ['|']['_' prefix ':' nextnode]*

    If the node is a final node (i.e. a valid word is completed at
    the node), the first character in the line is
    a vertical bar ('|') followed by an underscore.
    The rest of the line is a sequence of edges where each edge
    is described by a prefix string followed by a colon (':')
    and the line number of the node following that edge. Edges are
    separated by underscores ('_'). The prefix string can contain
    embedded vertical bars indicating that the previous character was
    a final character in a valid word.

    Example:

    The following input word list (cf. http://tinyurl.com/kvhbyo2):

    car
    cars
    cat
    cats
    do
    dog
    dogs
    done
    ear
    ears
    eat
    eats

    generates this output graph:

    do:3_ca:2_ea:2
    t|s:0_r|s:0
    |_g|s:0_ne:0

    The root node in line 1 has three outgoing edges, "do" to node 3, "ca" to node 2, and "ea" to node 2.

    Node 2 (in line 2) has two edges, "t|s" to node 0 and "r|s" to node 0. This means that "cat" and
    "cats", "eat" and "eats" are valid words (on the first edge), as well as "car" and "cars",
    "ear" and "ears" (on the second edge).

    Node 3 (in line 3) is itself a final node, denoted by the vertical bar at the start of the line.
    Thus, "do" (coming in from the root) is a valid word, but so are "dog" and "dogs" (on the first edge)
    as well as "done" (on the second edge).

    Dictionary structure:

    Suppose the dictionary contains two words, 'word' and 'wolf'.
    This is represented by Python data structures as follows:

    root _Dawg -> {
        'w': _DawgNode(final=False, edges -> {
            'o': _DawgNode(final=False, edges -> {
                'r': _DawgNode(final=False, edges -> {
                    'd': _DawgNode(final=True, edges -> {})
                    }),
                'l': _DawgNode(final=False, edges -> {
                    'f': _DawgNode(final=True, edges -> {})
                    })
                })
            })
        }

"""

import os
import codecs
import locale

import binascii
import struct
import io

from dawgdictionary import DawgDictionary

from languages import Alphabet

MAXLEN = 48 # Longest possible word to be processed
SCRABBLE_MAXLEN = 15 # Longest possible word in a Scrabble database

class _DawgNode:

    """ A _DawgNode is a node in a Directed Acyclic Word Graph (DAWG).
        It contains:
            * a node identifier (a simple unique sequence number);
            * a dictionary of edges (children) where each entry has a prefix
                (following letter(s)) together with its child _DawgNode;
            * and a Bool (final) indicating whether this node in the graph
                also marks the end of a legal word.

        A _DawgNode has a string representation which can be hashed to
        determine whether it is identical to a previously encountered node,
        i.e. whether it has the same final flag and the same edges with
        prefixes leading to the same child nodes. This assumes
        that the child nodes have already been subjected to the same
        test, i.e. whether they are identical to previously encountered
        nodes and, in that case, modified to point to the previous, identical
        subgraph. Each graph layer can thus depend on the (shallow) comparisons
        made in previous layers and deep comparisons are not necessary. This
        is an important optimization when building the graph.

    """

    # Running count of node identifiers
    # Zero is reserved for "None"
    _nextid = 1

    @staticmethod
    def stringify_edges(edges, arr):
        """ Utility function to create a compact descriptor string and hashable key for node edges """
        for prefix, node in edges.items():
            arr.append(prefix + u':' + (u'0' if node is None else str(node.id)))
        return "_".join(arr)

    def __init__(self):
        self.id = _DawgNode._nextid
        _DawgNode._nextid += 1
        self.edges = dict()
        self.final = False
        self._strng = None # Cached string representation of this node
        self._hash = None # Hash of the final flag and a shallow traversal of the edges

    def __str__(self):
        """ Return a string representation of this node, cached if possible """
        if not self._strng:
            # We don't have a cached string representation: create it
            arr = []
            if self.final: 
                arr.append("|")
            self._strng = _DawgNode.stringify_edges(self.edges, arr)
        return self._strng

    def __hash__(self):
        """ Return a hash of this node, cached if possible """
        if not self._hash:
            # We don't have a cached hash: create it
            self._hash = self.__str__().__hash__()
        return self._hash

    def __eq__(self, other):
        """ Use string equality based on the string representation of nodes """
        return self.__str__() == other.__str__()

    def reset_id(self, newid):
        """ Set a new id number for this node. This forces a reset of the cached data. """
        self.id = newid
        self._strng = None
        self._hash = None


class _Dawg:

    def __init__(self):
        self._lastword = u''
        self._lastlen = 0
        self._root = dict()
        # Initialize empty list of starting dictionaries
        self._dicts = [None] * MAXLEN
        self._dicts[0] = self._root
        # Initialize the result list of unique nodes
        self._unique_nodes = dict()

    def _collapse_branch(self, parent, prefix, node):
        """ Attempt to collapse a single branch of the tree """

        di = node.edges
        assert di is not None

        # If the node has no outgoing edges, it must be a final node.
        # Optimize and reduce graph clutter by making the parent
        # point to None instead.

        if len(di) == 0:
            assert node.final
            # We don't need to put a vertical bar (final marker) at the end of the prefix; it's implicit
            parent[prefix] = None
            return

        # Attempt to collapse simple chains of single-letter nodes
        # with single outgoing edges into a single edge with a multi-letter prefix.
        # If any of the chained nodes has a final marker, add a vertical bar '|' to
        # the prefix instead.

        if len(di) == 1:
            # Only one child: we can collapse
            lastd = None
            tail = None
            for ch, nx in di.items():
                # There will only be one iteration of this loop
                tail = ch
                lastd = nx
            # Delete the child node and put a string of prefix characters into the root instead
            del parent[prefix]
            if node.final:
                tail = u'|' + tail
            prefix += tail
            parent[prefix] = lastd
            node = lastd

        # If a node with the same signature (key) has already been generated,
        # i.e. having the same final flag and the same edges leading to the same
        # child nodes, replace the edge leading to this node with an edge
        # to the previously generated node.

        if node in self._unique_nodes:
            # Signature matches a previously generated node: replace the edge
            parent[prefix] = self._unique_nodes[node]
        else:
            # This is a new, unique signature: store it in the dictionary of unique nodes
            self._unique_nodes[node] = node

    def _collapse(self, edges):
        """ Collapse and optimize the edges in the parent dict """
        # Iterate through the letter position and
        # attempt to collapse all "simple" branches from it
        for letter, node in edges.items():
            if node:
                self._collapse_branch(edges, letter, node)

    def _collapse_to(self, divergence):
        """ Collapse the tree backwards from the point of divergence """
        j = self._lastlen
        while j > divergence:
            if self._dicts[j]:
                self._collapse(self._dicts[j])
                self._dicts[j] = None
            j -= 1

    def add_word(self, wrd):
        """ Add a word to the DAWG.
            Words are expected to arrive in sorted order.

            As an example, we may have these three words arriving in sequence:

            abbadísar
            abbadísarinnar  [extends last word by 5 letters]
            abbadísarstofa  [backtracks from last word by 5 letters]

        """
        # Sanity check: make sure the word is not too long
        lenword = len(wrd)
        if lenword >= MAXLEN:
            raise ValueError("Word exceeds maximum length of {0} letters".format(MAXLEN))
        # First see how many letters we have in common with the
        # last word we processed
        i = 0
        while i < lenword and i < self._lastlen and wrd[i] == self._lastword[i]:
            i += 1
        # Start from the point of last divergence in the tree
        # In the case of backtracking, collapse all previous outstanding branches
        self._collapse_to(i)
        # Add the (divergent) rest of the word
        d = self._dicts[i] # Note that self._dicts[0] is self._root
        nd = None
        while i < lenword:
            nd = _DawgNode()
            # Add a new starting letter to the working dictionary,
            # with a fresh node containing an empty dictionary of subsequent letters
            d[wrd[i]] = nd
            d = nd.edges
            i += 1
            self._dicts[i] = d
        # We are at the node for the final letter in the word: mark it as such
        if nd is not None:
            nd.final = True
        # Save our position to optimize the handling of the next word
        self._lastword = wrd
        self._lastlen = lenword

    def finish(self):
        """ Complete the optimization of the tree """
        self._collapse_to(0)
        self._lastword = u''
        self._lastlen = 0
        self._collapse(self._root)
        # Renumber the nodes for a tidier graph and more compact output
        # 1 is the line number of the root in text output files, so we start with 2
        ix = 2
        for n in self._unique_nodes.values():
            if n is not None:
                n.reset_id(ix)
                ix += 1

    def _dump_level(self, level, d):
        """ Dump a level of the tree and continue into sublevels by recursion """
        for ch, nx in d.items():
            s = u' ' * level + ch
            if nx and nx.final:
                s = s + u'|'
            s += u' ' * (50 - len(s))
            s += nx.__str__()
            print(s)
            if nx and nx.edges:
                self._dump_level(level + 1, nx.edges)

    def dump(self):
        """ Write a human-readable text representation of the DAWG to the standard output """
        self._dump_level(0, self._root)
        print("Total of {0} nodes and {1} edges with {2} prefix characters".format(self.num_unique_nodes(),
            self.num_edges(), self.num_edge_chars()))
        for ix, n in enumerate(self._unique_nodes.values()):
            if n is not None:
                # We don't use ix for the time being
                print(u"Node {0}{1}".format(n.id, u"|" if n.final else u""))
                for prefix, nd in n.edges.items():
                    print(u"   Edge {0} to node {1}".format(prefix, 0 if nd is None else nd.id))

    def num_unique_nodes(self):
        """ Count the total number of unique nodes in the graph """
        return len(self._unique_nodes)

    def num_edges(self):
        """ Count the total number of edges between unique nodes in the graph """
        edges = 0
        for n in self._unique_nodes.values():
            if n is not None:
                edges += len(n.edges)
        return edges

    def num_edge_chars(self):
        """ Count the total number of edge prefix letters in the graph """
        chars = 0
        for n in self._unique_nodes.values():
            if n is not None:
                for prefix in n.edges:
                    # Add the length of all prefixes to the edge, minus the vertical bar
                    # '|' which indicates a final character within the prefix
                    chars += len(prefix) - prefix.count(u'|')
        return chars

    def write_packed(self, packer):
        """ Write the optimized DAWG to a packer """
        packer.start(len(self._root))
        # Start with the root edges
        for prefix, nd in self._root.items():
            packer.edge(nd.id, prefix)
        for node in self._unique_nodes.values():
            if node is not None:
                packer.node_start(node.id, node.final, len(node.edges))
                for prefix, nd in node.edges.items():
                    if nd is None:
                        packer.edge(0, prefix)
                    else:
                        packer.edge(nd.id, prefix)
                packer.node_end(node.id)
        packer.finish()

    def write_text(self, stream):
        """ Write the optimized DAWG to a text stream """
        print("Output graph has {0} nodes".format(len(self._unique_nodes))) # +1 to include the root in the node count
        # We don't have to write node ids since they correspond to line numbers.
        # The root is always in the first line and the first node after the root has id 2.
        # Start with the root edges
        arr = []
        stream.write(_DawgNode.stringify_edges(self._root, arr) + u"\n")
        for node in self._unique_nodes.values():
            if node is not None:
                stream.write(node.__str__() + u"\n")

class _BinaryDawgPacker:

    """ _BinaryDawgPacker packs the DAWG data to a byte stream.

        !!! This is not fully implemented and not currently used by the
        !!! DawgDictionary class in dawgdictionary.py

        The stream format is as follows:

        For each node:
            BYTE Node header
                [feeeeeee]
                    f = final bit
                    eeee = number of edges
            For each edge out of a node:
                BYTE Prefix header
                    [ftnnnnnn]
                    If t == 1 then
                        f = final bit of single prefix character
                        nnnnnn = single prefix character,
                            coded as an index into AÁBDÐEÉFGHIÍJKLMNOÓPRSTUÚVXYÝÞÆÖ
                    else
                        00nnnnnn = number of prefix characters following
                        n * BYTE Prefix characters
                            [fccccccc]
                                f = final bit
                                ccccccc = prefix character,
                                    coded as an index into AÁBDÐEÉFGHIÍJKLMNOÓPRSTUÚVXYÝÞÆÖ
                DWORD Offset of child node

    """

    CODING_UCASE = Alphabet.upper
    CODING_LCASE = Alphabet.order

    def __init__(self, stream):
        self._stream = stream
        self._byte_struct = struct.Struct("<B")
        self._loc_struct = struct.Struct("<L")
        # _locs is a dict of already written nodes and their stream locations
        self._locs = dict()
        # _fixups is a dict of node ids and file positions where the
        # node id has been referenced without knowing where the node is
        # located
        self._fixups = dict()

    def start(self, num_root_edges):
        # The stream starts off with a single byte containing the
        # number of root edges
        self._stream.write(self._byte_struct.pack(num_root_edges))

    def node_start(self, id, final, num_edges):
        pos = self._stream.tell()
        if id in self._fixups:
            # We have previously output references to this node without
            # knowing its location: fix'em now
            for fix in self._fixups[id]:
                self._stream.seek(fix)
                self._stream.write(self._loc_struct.pack(pos))
            self._stream.seek(pos)
            del self._fixups[id]
        # Remember where we put this node
        self._locs[id] = pos
        self._stream.write(self._byte_struct.pack((0x80 if final else 0x00) | (num_edges & 0x7F)))

    def node_end(self, id):
        pass

    def edge(self, id, prefix):
        b = []
        last = None
        for c in prefix:
            if c == u'|':
                last |= 0x80
            else:
                if last is not None:
                    b.append(last)
                try:
                    last = _BinaryDawgPacker.CODING_LCASE.index(c)
                except ValueError:
                    last = _BinaryDawgPacker.CODING_UCASE.index(c)
        b.append(last)

        if len(b) == 1:
            # Save space on single-letter prefixes
            self._stream.write(self._byte_struct.pack(b[0] | 0x40))
        else:
            self._stream.write(self._byte_struct.pack(len(b) & 0x3F))
            for by in b:
                self._stream.write(self._byte_struct.pack(by))
        if id == 0:
            self._stream.write(self._loc_struct.pack(0))
        elif id in self._locs:
            # We've already written the node and know where it is: write its location
            self._stream.write(self._loc_struct.pack(self._locs[id]))
        else:
            # This is a forward reference to a node we haven't written yet:
            # reserve space for the node location and add a fixup
            pos = self._stream.tell()
            self._stream.write(self._loc_struct.pack(0xFFFFFFFF)) # Temporary - will be overwritten
            if id not in self._fixups:
                self._fixups[id] = []
            self._fixups[id].append(pos)

    def finish(self):
        # Clear the temporary fixup stuff from memory
        self._locs = dict()
        self._fixups = dict()

    def dump(self):
        buf = self._stream.getvalue()
        print("Total of {0} bytes".format(len(buf)))
        s = binascii.hexlify(buf)
        BYTES_PER_LINE = 16
        CHARS_PER_LINE = BYTES_PER_LINE * 2
        i = 0
        addr = 0
        lens = len(s)
        while i < lens:
            line = s[i : i + CHARS_PER_LINE]
            print("{0:08x}: {1}".format(addr, u" ".join([line[j : j + 2] for j in range(0, len(line) - 1, 2)])))
            i += CHARS_PER_LINE
            addr += BYTES_PER_LINE


class DawgBuilder:

    """ Creates a DAWG from word lists and writes the resulting
        graph to binary or text files.

        The word lists are assumed to be pre-sorted in ascending
        lexicographic order. They are automatically merged during
        processing to appear as one aggregated and sorted word list.
    """

    def __init__(self):
        self._dawg = None

    class _InFile:
        """ InFile represents a single sorted input file. """

        def __init__(self, relpath, fname):
            self._eof = False
            self._nxt = None
            fpath = os.path.abspath(os.path.join(relpath, fname))
            self._fin = codecs.open(fpath, mode='r', encoding='utf-8')
            print(u"Opened input file {0}".format(fpath))
            # Read the first word from the file to initialize the iteration
            self.read_word()

        def read_word(self):
            """ Read lines until we have a legal word or EOF """
            while True:
                try:
                    line = self._fin.next()
                except StopIteration:
                    # We're done with this file
                    self._eof = True
                    return False
                if line.endswith(u'\r\n'):
                    # Cut off trailing CRLF (Windows-style)
                    line = line[0:-2]
                elif line.endswith(u'\n'):
                    # Cut off trailing LF (Unix-style)
                    line = line[0:-1]
                if line and len(line) < MAXLEN:
                    # Valid word
                    self._nxt = line
                    return True

        def next_word(self):
            """ Returns the next available word from this input file """
            return None if self._eof else self._nxt

        def has_word(self):
            """ True if a word is available, or False if EOF has been reached """
            return not self._eof

        def close(self):
            """ Close the associated file, if it is still open """
            if self._fin is not None:
                self._fin.close()
            self._fin = None

    def _load(self, relpath, inputs, localeid, filter):
        """ Load word lists into the DAWG from one or more static text files,
            assumed to be located in the relpath subdirectory.
            The text files should contain one word per line,
            encoded in UTF-8 format. Lines may end with CR/LF or LF only.
            Upper or lower case should be consistent throughout.
            All lower case is preferred. The words should appear in
            ascending sort order within each file. The input files will
            be merged in sorted order in the load process.
        """
        self._dawg = _Dawg()
        # Total number of words read from input files
        incount = 0
        # Total number of words written to output file
        # (may be less than incount because of filtering or duplicates)
        outcount = 0
        # Total number of duplicate words found in input files
        duplicates = 0
        # If a locale for sorting/collation is specified, set it
        if localeid:
            locale.setlocale(locale.LC_COLLATE, localeid)
        # Enforce strict ascending lexicographic order
        lastword = None
        # Open the input files
        infiles = [DawgBuilder._InFile(relpath, f) for f in inputs]
        # Merge the inputs
        while True:
            smallest = None
            # Find the smallest next word among the input files
            for f in infiles:
                if f.has_word():
                    if smallest is None:
                        smallest = f
                    else:
                        # Use the sort ordering of the current locale to compare words
                        cmp = locale.strcoll(f.next_word(), smallest.next_word())
                        if cmp == 0:
                            # We have the same word in two files: make sure we don't add it twice
                            f.read_word()
                            incount += 1
                            duplicates += 1
                        elif cmp < 0:
                            # New smallest word
                            smallest = f
            if smallest is None:
                # All files exhausted: we're done
                break
            # We have the smallest word
            word = smallest.next_word()
            incount += 1
            if lastword and locale.strcoll(lastword, word) >= 0:
                # Something appears to be wrong with the input sort order.
                # If it's a duplicate, we don't mind too much, but if it's out
                # of order, display a warning
                if locale.strcoll(lastword, word) > 0:
                    print(u"Warning: input files should be in ascending order, but \"{0}\" > \"{1}\"".format(lastword, word))
                else:
                    duplicates += 1
            elif (filter is None) or filter(word):
                # This word passes the filter: add it to the graph
                self._dawg.add_word(word)
                lastword = word
                outcount += 1
            if incount % 5000 == 0:
                # Progress indicator
                print ("{0}...\r".format(incount)),
            # Advance to the next word in the file we read from
            smallest.read_word()
        # Done merging: close all files
        for f in infiles:
            assert not f.has_word()
            f.close()
            f = None
        # Complete and clean up
        self._dawg.finish()
        print("Finished loading {0} words, output {1} words, {2} duplicates skipped".format(incount, outcount, duplicates))

    def _output_binary(self, relpath, output):
        """ Write the DAWG to a flattened binary output file with extension '.dawg' """
        assert self._dawg is not None
        # !!! Experimental / debugging...
        f = io.BytesIO()
        # Create a packer to flatten the tree onto a binary stream
        p = _BinaryDawgPacker(f)
        # Write the tree using the packer
        self._dawg.write_packed(p)
        # Dump the packer contents to stdout for debugging
        p.dump()
        # Write packed DAWG to binary file
        with open(os.path.abspath(os.path.join(relpath, output + u".dawg")), "wb") as of:
            of.write(f.getvalue())
        f.close()

    def _output_text(self, relpath, output):
        """ Write the DAWG to a text output file with extension '.text.dawg' """
        assert self._dawg is not None
        fname = os.path.abspath(os.path.join(relpath, output + u".text.dawg"))
        with codecs.open(fname, mode='w', encoding='utf-8') as fout:
            self._dawg.write_text(fout)

    def build(self, inputs, output, relpath="resources", localeid=None, filter=None):
        """ Build a DAWG from input file(s) and write it to the output file(s) (potentially in multiple formats).
            The input files are assumed to be individually sorted in correct ascending alphabetical
            order. They will be merged in parallel into a single sorted stream and added to the DAWG.
        """
        # inputs is a list of input file names
        # output is an output file name without file type suffix (extension);
        # ".dawg" and ".text.dawg" will be appended depending on output formats
        # relpath is a relative path to the input and output files
        print("DawgBuilder starting...")
        if (not inputs) or (not output):
            # Nothing to do
            print("No inputs or no output: Nothing to do")
            return
        self._load(relpath, inputs, localeid, filter)
        # print("Dumping...")
        # self._dawg.dump()
        print("Outputting...")
        # self._output_binary(relpath, output) # Not used for now
        self._output_text(relpath, output)
        print("DawgBuilder done")

# Filter functions
# The resulting DAWG will include all words for which filter() returns True, and exclude others.
# Useful for excluding long words or words containing "foreign" characters.

def nofilter(word):
    """ No filtering - include all input words in output graph """
    return True

# disallowed = set([u"je", u"oj", u"pé"]) # Previously had u"im" and u"ím" here

def filter_skrafl(word):
    """ Filtering for Icelandic Scrabble(tm)
        Exclude words longer than SCRABBLE_MAXLEN letters (won't fit on board)
        Exclude words with non-Icelandic letters, i.e. C, Q, W, Z
        Exclude two-letter words in the word database that are not
            allowed according to Icelandic Scrabble rules
    """
    lenw = len(word)
    if lenw > SCRABBLE_MAXLEN:
        # Too long, not necessary for Scrabble
        return False
#    if any(c in word for c in u"cqwzü"):
#        # Non-Icelandic character
#        return False
#    if word in disallowed:
#        # Two-letter word that is not allowed in Icelandic Scrabble
#        return False
    return True

import time

def run_test():
    """ Build a DAWG from the files listed """
    # This creates a DAWG from a single file named testwords.txt
    print(u"Starting DAWG build for testwords.txt")
    db = DawgBuilder()
    t0 = time.time()
    db.build(
        ["testwords.txt"], # Input files to be merged
        "testwords", # Output file - full name will be testwords.text.dawg
        "resources") # Subfolder of input and output files
    t1 = time.time()
    print("Build took {0:.2f} seconds".format(t1 - t0))

def run_twl06():
    """ Build a DAWG from the files listed """
    # This creates a DAWG from a single file named TWL06.txt,
    # the Scrabble Tournament Word List version 6
    print(u"Starting DAWG build for TWL06.txt")
    db = DawgBuilder()
    t0 = time.time()
    db.build(
        ["TWL06.txt"], # Input files to be merged
        "TWL06", # Output file - full name will be TWL06.text.dawg
        "resources") # Subfolder of input and output files
    t1 = time.time()
    print("Build took {0:.2f} seconds".format(t1 - t0))

def run_full_bin():
    """ Build a DAWG from the files listed """
    # This creates a DAWG from the full database of 'Beygingarlýsing íslensks nútímamáls' (BIN)
    # (except abbreviations, 'skammstafanir', and proper names, 'sérnöfn')
    # This is about 2.6 million words, generating 103,200 graph nodes
    print(u"Starting DAWG build for Beygingarlýsing íslensks nútímamáls")
    db = DawgBuilder()
    t0 = time.time()
    # "isl" specifies Icelandic sorting order - modify this for other languages
    db.build(
        ["ordalisti1.txt", "ordalisti2.txt", "smaord.sorted.txt"], # Input files to be merged
        "ordalisti-bin", # Output file - full name will be ordalisti-bin.text.dawg
        "resources", # Subfolder of input and output files
        "isl", # Identifier of locale to use for sorting order
        nofilter) # Word filter function to apply
    t1 = time.time()
    print("Build took {0:.2f} seconds".format(t1 - t0))

def run_skrafl():
    """ Build a DAWG from the files listed """
    # This creates a DAWG from the full database of 'Beygingarlýsing íslensks nútímamáls' (BIN)
    # (except abbreviations, 'skammstafanir', and proper names, 'sérnöfn'), but
    # filtered according to filter_skrafl() (see comment above) and with a few two-letter
    # words added according to Icelandic Scrabble rules
    # Output is about 2.2 million words, generating 102,668 graph nodes
    print(u"Starting DAWG build for skraflhjalp/netskrafl.appspot.com")
    db = DawgBuilder()
    t0 = time.time()
    # "isl" specifies Icelandic sorting order - modify this for other languages
    db.build(
        ["ordalistimax15.sorted.txt"], # Input files to be merged
        "ordalisti", # Output file - full name will be ordalisti.text.dawg
        "resources", # Subfolder of input and output files
        "isl", # Identifier of locale to use for sorting order
        filter_skrafl) # Word filter function to apply
    t1 = time.time()
    print("Build took {0:.2f} seconds".format(t1 - t0))

    dawg = DawgDictionary()
    fpath = os.path.abspath(os.path.join("resources", "ordalisti.text.dawg"))
    t0 = time.time()
    dawg.load(fpath)
    t1 = time.time()

    print("DAWG loaded in {0:.2f} seconds".format(t1 - t0))

    t0 = time.time()
    dawg.store_pickle(os.path.abspath(os.path.join("resources", "ordalisti.dawg.pickle")))
    t1 = time.time()

    print("DAWG pickle file stored in {0:.2f} seconds".format(t1 - t0))


if __name__ == '__main__':

    run_skrafl()
