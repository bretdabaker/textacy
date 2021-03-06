# -*- coding: utf-8 -*-
"""
Object-oriented interface for processing individual text documents as well as
collections (corpora).
"""
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import Counter
import copy
import re

from cytoolz import itertoolz
from spacy.tokens.doc import Doc as sdoc
from spacy.tokens.token import Token as stoken
from spacy.tokens.span import Span as sspan

from textacy.compat import str, zip
from textacy import data, extract, spacy_utils, text_stats, text_utils, keyterms
from textacy.representations import network, vsm


class TextDoc(object):
    """
    Pairing of the content of a document with its associated metadata, where the
    content has been tokenized, tagged, parsed, etc. by spaCy. Also keep references
    to the id-to-token mapping used by spaCy to efficiently represent the content.
    If initialized from plain text, this processing is performed automatically.

    ``TextDoc`` also provides a convenient interface to information extraction,
    different doc representations, statistical measures of the content, and more.

    Args:
        text_or_sdoc (str or ``spacy.Doc``): text or spacy doc containing the
            content of this ``TextDoc``; if str, it is automatically processed
            by spacy before assignment to the ``TextDoc.spacy_doc`` attribute
        spacy_pipeline (``spacy.<lang>.<Lang>()``, optional): if a spacy pipeline
            has already been loaded or used to make the input ``spacy.Doc``, pass
            it in here to speed things up a touch; in general, only one of these
            pipelines should be loaded per process
        lang (str, optional): if doc's language is known, give its 2-letter code
            (https://cloud.google.com/translate/v2/using_rest#language-params);
            if None (default), lang will be automatically inferred from text
        metadata (dict, optional): dictionary of relevant information about the
            input ``text_or_sdoc``, e.g.::

                {'title': 'A Search for Second-generation Leptoquarks in pp Collisions at √s = 7 TeV with the ATLAS Detector',
                 'author': 'Burton DeWilde', 'pub_date': '2012-08-01'}
    """
    def __init__(self, text_or_sdoc, spacy_pipeline=None, lang=None, metadata=None):
        self.metadata = {} if metadata is None else metadata
        self._term_counts = Counter()

        if isinstance(text_or_sdoc, str):
            self.lang = text_utils.detect_language(text_or_sdoc) if not lang else lang
            if spacy_pipeline is None:
                spacy_pipeline = data.load_spacy(self.lang)
            # check for match between text and passed spacy_pipeline language
            else:
                if spacy_pipeline.lang != self.lang:
                    msg = 'TextDoc.lang {} != spacy_pipeline.lang {}'.format(
                        self.lang, spacy_pipeline.lang)
                    raise ValueError(msg)
            self.spacy_vocab = spacy_pipeline.vocab
            self.spacy_stringstore = self.spacy_vocab.strings
            self.spacy_doc = spacy_pipeline(text_or_sdoc)

        elif isinstance(text_or_sdoc, sdoc):
            self.lang = spacy_pipeline.lang if spacy_pipeline is not None else \
                text_utils.detect_language(text_or_sdoc.text_with_ws)
            self.spacy_vocab = text_or_sdoc.vocab
            self.spacy_stringstore = self.spacy_vocab.strings
            self.spacy_doc = text_or_sdoc

        else:
            msg = 'TextDoc must be initialized with {}, not {}'.format(
                {str, sdoc}, type(text_or_sdoc))
            raise ValueError(msg)

    def __repr__(self):
        snippet = self.text[:50].replace('\n',' ')
        if len(snippet) == 50:
            snippet = snippet[:47] + '...'
        return 'TextDoc({} tokens; "{}")'.format(len(self.spacy_doc), snippet)

    def __len__(self):
        return self.n_tokens

    def __getitem__(self, index):
        return self.spacy_doc[index]

    def __iter__(self):
        for tok in self.spacy_doc:
            yield tok

    @property
    def tokens(self):
        """Yield the document's tokens as tokenized by spacy; same as ``__iter__``."""
        for tok in self.spacy_doc:
            yield tok

    @property
    def sents(self):
        """Yield the document's sentences as segmented by spacy."""
        for sent in self.spacy_doc.sents:
            yield sent

    def merge(self, spans):
        """
        Merge spans *in-place* within doc so that each takes up a single token.
        Note: All cached methods on this doc will be cleared.

        Args:
            spans (iterable(``spacy.Span``)): for example, the results from
                :func:`extract.named_entities() <textacy.extract.named_entities>`
                or :func:`extract.pos_regex_matches() <textacy.extract.pos_regex_matches>`
        """
        spacy_utils.merge_spans(spans)

    ###############
    # DOC AS TEXT #

    @property
    def text(self):
        """Return the document's raw text."""
        return self.spacy_doc.text_with_ws

    @property
    def tokenized_text(self):
        """Return text as an ordered, nested list of tokens per sentence."""
        return [[token.text for token in sent]
                for sent in self.spacy_doc.sents]

    @property
    def pos_tagged_text(self):
        """Return text as an ordered, nested list of (token, POS) pairs per sentence."""
        return [[(token.text, token.pos_) for token in sent]
                for sent in self.spacy_doc.sents]

    #######################
    # DOC REPRESENTATIONS #

    def as_bag_of_terms(self, weighting='tf', normalized=True, binary=False,
                        idf=None, lemmatize='auto',
                        ngram_range=(1, 1),
                        include_nes=False, include_ncs=False, include_kts=False):
        """
        Represent doc as a "bag of terms", an unordered set of (term id, term weight)
        pairs, where term weight may be by TF or TF*IDF.

        Args:
            weighting (str {'tf', 'tfidf'}, optional): weighting of term weights,
                either term frequency ('tf') or tf * inverse doc frequency ('tfidf')
            idf (dict, optional): if `weighting` = 'tfidf', idf's must be supplied
                externally, such as from a `TextCorpus` object
            lemmatize (bool or 'auto', optional): if True, lemmatize all terms
                when getting their frequencies
            ngram_range (tuple(int), optional): (min n, max n) values for n-grams
                to include in terms list; default (1, 1) only includes unigrams
            include_nes (bool, optional): if True, include named entities in terms list
            include_ncs (bool, optional): if True, include noun chunks in terms list
            include_kts (bool, optional): if True, include key terms in terms list
            normalized (bool, optional): if True, normalize term freqs by the
                total number of unique terms
            binary (bool optional): if True, set all (non-zero) term freqs equal to 1

        Returns:
            :class:`collections.Counter <collections.Counter>`: mapping of term ids
                to corresponding term weights
        """
        term_weights = self.term_counts(
            lemmatize=lemmatize, ngram_range=ngram_range, include_nes=include_nes,
            include_ncs=include_ncs, include_kts=include_kts)

        if binary is True:
            term_weights = Counter({key: 1 for key in term_weights.keys()})
        elif normalized is True:
            # n_terms = sum(term_freqs.values())
            n_tokens = self.n_tokens
            term_weights = Counter({key: val / n_tokens
                                    for key, val in term_weights.items()})

        if weighting == 'tfidf' and idf:
            term_weights = Counter({key: val * idf[key]
                                    for key, val in term_weights.items()})

        return term_weights

    def as_semantic_network(self, nodes='terms',
                            edge_weighting='default', window_width=10):
        """
        Represent doc as a semantic network, where nodes are either 'terms' or
        'sents', and edges between nodes may be weighted in different ways.

        Args:
            nodes (str, {'terms', 'sents'}): type of doc component to use as nodes
                in the semantic network
            edge_weighting (str, optional): type of weighting to apply to edges
                between nodes; if ``nodes == 'terms'``, options are {'cooc_freq', 'binary'},
                if ``nodes == 'sents'``, options are {'cosine', 'jaccard'}; if
                'default', 'cooc_freq' or 'cosine' will be automatically used
            window_width (int, optional): size of sliding window over terms that
                determines which are said to co-occur; only applicable if 'terms'

        Returns:
            :class:`networkx.Graph <networkx.Graph>`: where nodes represent either
                terms or sentences in doc; edges, the relationships between them

        .. seealso:: :func:`network.terms_to_semantic_network() <textacy.representations.network.terms_to_semantic_network>`
        .. seealso:: :func:`network.sents_to_semantic_network() <textacy.representations.network.sents_to_semantic_network>`
        """
        if nodes == 'terms':
            if edge_weighting == 'default':
                edge_weighting = 'cooc_freq'
            return network.terms_to_semantic_network(
                list(self.words()), window_width=window_width,
                edge_weighting=edge_weighting)
        elif nodes == 'sents':
            if edge_weighting == 'default':
                edge_weighting = 'cosine'
            return network.sents_to_semantic_network(
                list(self.sents), edge_weighting=edge_weighting)
        else:
            msg = 'nodes "{}" not valid; must be in {}'.format(nodes, {'terms', 'sents'})
            raise ValueError(msg)

    def as_terms_list(self, words=True, ngrams=(2, 3), named_entities=True,
                      dedupe=True, lemmatize=True, **kwargs):
        """
        Represent doc as a sequence of terms -- which aren't necessarily in order --
        including words (unigrams), ngrams (for a range of n), and named entities.
        NOTE: Despite the name, this is a generator function; to get a *list* of terms,
        just wrap the call like ``list(doc.as_terms_list())``.

        Args:
            words (bool, optional): if True (default), include words in the terms list
            ngrams (tuple(int), optional): include a range of ngrams in the terms list;
                default is ``(2, 3)``, i.e. bigrams and trigrams are included; if
                ngrams aren't wanted, set to False-y

                NOTE: if n=1 (words) is included here and ``words`` is True, n=1 is skipped
            named_entities (bool, optional): if True (default), include named entities
                in the terms list
            dedupe (bool, optional): if True (default), named entities are added first
                to the terms list, and any words or ngrams that exactly overlap with
                previously added entities are skipped to prevent double-counting;
                since words and ngrams (n > 1) are inherently exclusive, this only
                applies to entities; you almost certainly want this to be True
            lemmatize (bool, optional): if True (default), lemmatize all terms;
                otherwise, return the text as it appeared
            kwargs:
                filter_stops (bool)
                filter_punct (bool)
                filter_nums (bool)
                good_pos_tags (set(str))
                bad_pos_tags (set(str))
                min_freq (int)
                good_ne_types (set(str))
                bad_ne_types (set(str))
                drop_determiners (bool)

        Yields:
            str: the next term in the terms list
        """
        all_terms = []
        # special case: ensure that named entities aren't double-counted when
        # adding words or ngrams that were already added as named entities
        if dedupe is True and named_entities is True and (words is True or ngrams):
            ents = list(self.named_entities(**kwargs))
            ent_idxs = {(ent.start, ent.end) for ent in ents}
            all_terms.append(ents)
            if words is True:
                all_terms.append((word for word in self.words(**kwargs)
                                  if (word.idx, word.idx + 1) not in ent_idxs))
            if ngrams:
                for n in range(ngrams[0], ngrams[1] + 1):
                    if n == 1 and words is True:
                        continue
                    all_terms.append((ngram for ngram in self.ngrams(n, **kwargs)
                                      if (ngram.start, ngram.end) not in ent_idxs))
        # otherwise add everything in, duplicates and all
        else:
            if named_entities is True:
                all_terms.append(self.named_entities(**kwargs))
            if words is True:
                all_terms.append(self.words(**kwargs))
            if ngrams:
                for n in range(ngrams[0], ngrams[1] + 1):
                    if n == 1 and words is True:
                        continue
                    all_terms.append(self.ngrams(n, **kwargs))

        if lemmatize is True:
            for term in itertoolz.concat(all_terms):
                yield term.lemma_
        else:
            for term in itertoolz.concat(all_terms):
                yield term.text

    ##########################
    # INFORMATION EXTRACTION #

    def words(self, **kwargs):
        """
        Extract an ordered sequence of words from a spacy-parsed doc, optionally
        filtering words by part-of-speech (etc.) and frequency.

        .. seealso:: :func:`extract.words() <textacy.extract.words>` for all function kwargs.
        """
        return extract.words(self.spacy_doc, **kwargs)

    def ngrams(self, n, **kwargs):
        """
        Extract an ordered sequence of n-grams (``n`` consecutive words) from doc,
        optionally filtering n-grams by the types and parts-of-speech of the
        constituent words.

        Args:
            n (int): number of tokens to include in n-grams;
                1 => unigrams, 2 => bigrams

        .. seealso:: :func:`extract.ngrams() <textacy.extract.ngrams>` for all function kwargs.
        """
        return extract.ngrams(self.spacy_doc, n, **kwargs)

    def named_entities(self, **kwargs):
        """
        Extract an ordered sequence of named entities (PERSON, ORG, LOC, etc.) from
        doc, optionally filtering by the entity types and frequencies.

        .. seealso:: :func:`extract.named_entities() <textacy.extract.named_entities>`
        for all function kwargs.
        """
        return extract.named_entities(self.spacy_doc, **kwargs)

    def noun_chunks(self, **kwargs):
        """
        Extract an ordered sequence of noun phrases from doc, optionally
        filtering by frequency and dropping leading determiners.

        .. seealso:: :func:`extract.noun_chunks() <textacy.extract.noun_chunks>`
        for all function kwargs.
        """
        return extract.noun_chunks(self.spacy_doc, **kwargs)

    def pos_regex_matches(self, pattern):
        """
        Extract sequences of consecutive tokens from a spacy-parsed doc whose
        part-of-speech tags match the specified regex pattern.

        Args:
            pattern (str): Pattern of consecutive POS tags whose corresponding words
                are to be extracted, inspired by the regex patterns used in NLTK's
                ``nltk.chunk.regexp``. Tags are uppercase, from the universal tag set;
                delimited by < and >, which are basically converted to parentheses
                with spaces as needed to correctly extract matching word sequences;
                white space in the input doesn't matter.

                Examples (see :obj:`POS_REGEX_PATTERNS <textacy.regexes_etc.POS_REGEX_PATTERNS>`):

                * noun phrase: r'<DET>? (<NOUN>+ <ADP|CONJ>)* <NOUN>+'
                * compound nouns: r'<NOUN>+'
                * verb phrase: r'<VERB>?<ADV>*<VERB>+'
                * prepositional phrase: r'<PREP> <DET>? (<NOUN>+<ADP>)* <NOUN>+'
        """
        return extract.pos_regex_matches(self.spacy_doc, pattern)

    def subject_verb_object_triples(self):
        """
        Extract an *un*ordered sequence of distinct subject-verb-object (SVO) triples
        from doc.
        """
        return extract.subject_verb_object_triples(self.spacy_doc)

    def acronyms_and_definitions(self, **kwargs):
        """
        Extract a collection of acronyms and their most likely definitions,
        if available, from doc. If multiple definitions are found for a given acronym,
        only the most frequently occurring definition is returned.

        .. seealso:: :func:`extract.acronyms_and_definitions() <textacy.extract.acronyms_and_definitions>`
        for all function kwargs.
        """
        return extract.acronyms_and_definitions(self.spacy_doc, **kwargs)

    def semistructured_statements(self, entity, **kwargs):
        """
        Extract "semi-structured statements" from doc, each as a (entity, cue, fragment)
        triple. This is similar to subject-verb-object triples.

        Args:
            entity (str): a noun or noun phrase of some sort (e.g. "President Obama",
                "global warming", "Python")

        .. seealso:: :func:`extract.semistructured_statements() <textacy.extract.semistructured_statements>`
        for all function kwargs.
        """
        return extract.semistructured_statements(self.spacy_doc, entity, **kwargs)

    def direct_quotations(self):
        """
        Baseline, not-great attempt at direction quotation extraction (no indirect
        or mixed quotations) using rules and patterns. English only.
        """
        return extract.direct_quotations(self.spacy_doc)

    def key_terms(self, algorithm='sgrank', n=10):
        """
        Extract key terms from a document using `algorithm`.

        Args:
            algorithm (str {'sgrank', 'textrank', 'singlerank'}, optional): name
                of algorithm to use for key term extraction
            n (int or float, optional): if int, number of top-ranked terms to return
                as keyterms; if float, must be in the open interval (0.0, 1.0),
                representing the fraction of top-ranked terms to return as keyterms

        Raises:
            ValueError: if ``algorithm`` not in {'sgrank', 'textrank', 'singlerank'}
        """
        if algorithm == 'sgrank':
            return keyterms.sgrank(self.spacy_doc, window_width=1500, n_keyterms=n)
        elif algorithm == 'textrank':
            return keyterms.textrank(self.spacy_doc, n_keyterms=n)
        elif algorithm == 'singlerank':
            return keyterms.singlerank(self.spacy_doc, n_keyterms=n)
        else:
            raise ValueError('algorithm {} not a valid option'.format(algorithm))

    ##############
    # STATISTICS #

    def term_counts(self, lemmatize='auto', ngram_range=(1, 1),
                    include_nes=False, include_ncs=False, include_kts=False):
        """
        Get the number of occurrences ("counts") of each unique term in doc;
        terms may be words, n-grams, named entities, noun phrases, and key terms.

        Args:
            lemmatize (bool or 'auto', optional): if True, lemmatize all terms
                when getting their frequencies; if 'auto', lemmatize all terms
                that aren't proper nouns or acronyms
            ngram_range (tuple(int), optional): (min n, max n) values for n-grams
                to include in terms list; default (1, 1) only includes unigrams
            include_nes (bool, optional): if True, include named entities in terms list
            include_ncs (bool, optional): if True, include noun chunks in terms list
            include_kts (bool, optional): if True, include key terms in terms list

        Returns:
            :class:`collections.Counter() <collections.Counter>`: mapping of unique
                term ids to corresponding term counts
        """
        if lemmatize == 'auto':
            get_id = lambda x: self.spacy_stringstore[spacy_utils.normalized_str(x)]
        elif lemmatize is True:
            get_id = lambda x: self.spacy_stringstore[x.lemma_]
        else:
            get_id = lambda x: self.spacy_stringstore[x.text]

        for n in range(ngram_range[0], ngram_range[1] + 1):
            if n == 1:
                self._term_counts = self._term_counts | Counter(
                    get_id(word) for word in self.words())
            else:
                self._term_counts = self._term_counts | Counter(
                    get_id(ngram) for ngram in self.ngrams(n))
        if include_nes is True:
            self._term_counts = self._term_counts | Counter(
                get_id(ne) for ne in self.named_entities())
        if include_ncs is True:
            self._term_counts = self._term_counts | Counter(
                get_id(nc) for nc in self.noun_chunks())
        if include_kts is True:
            # HACK: key terms are currently returned as strings
            # TODO: cache key terms, and return them as spacy spans
            get_id = lambda x: self.spacy_stringstore[x]
            self._term_counts = self._term_counts | Counter(
                get_id(kt) for kt, _ in self.key_terms())

        return self._term_counts

    def term_count(self, term):
        """
        Get the number of occurrences ("count") of term in doc.

        Args:
            term (str or ``spacy.Token`` or ``spacy.Span``)

        Returns:
            int
        """
        # figure out what object we're dealing with here; convert as necessary
        if isinstance(term, str):
            term_text = term
            term_id = self.spacy_stringstore[term_text]
            term_len = term_text.count(' ') + 1
        elif isinstance(term, stoken):
            term_text = spacy_utils.normalized_str(term)
            term_id = self.spacy_stringstore[term_text]
            term_len = 1
        elif isinstance(term, sspan):
            term_text = spacy_utils.normalized_str(term)
            term_id = self.spacy_stringstore[term_text]
            term_len = len(term)

        term_count_ = self._term_counts[term_id]
        if term_count_ > 0:
            return term_count_
        # have we not already counted the appropriate `n` n-grams?
        if not any(self.spacy_stringstore[t].count(' ') == term_len
                   for t in self._term_counts):
            get_id = lambda x: self.spacy_stringstore[spacy_utils.normalized_str(x)]
            if term_len == 1:
                self._term_counts += Counter(get_id(w) for w in self.words())
            else:
                self._term_counts += Counter(get_id(ng) for ng in self.ngrams(term_len))
            term_count_ = self._term_counts[term_id]
            if term_count_ > 0:
                return term_count_
        # last resort: try a regular expression
        return sum(1 for _ in re.finditer(re.escape(term_text), self.text))

    @property
    def n_tokens(self):
        """The number of tokens in the document -- including punctuation."""
        return len(self.spacy_doc)

    @property
    def n_words(self):
        """
        The number of words in the document -- i.e. the number of tokens, excluding
        punctuation and whitespace.
        """
        return sum(1 for _ in self.words(filter_stops=False,
                                         filter_punct=True,
                                         filter_nums=False))

    @property
    def n_sents(self):
        """The number of sentences in the document."""
        return sum(1 for _ in self.spacy_doc.sents)

    def n_paragraphs(self, pattern=r'\n\n+'):
        """The number of paragraphs in the document, as delimited by ``pattern``."""
        return sum(1 for _ in re.finditer(pattern, self.text)) + 1

    @property
    def readability_stats(self):
        return text_stats.readability_stats(self)


class TextCorpus(object):
    """
    An ordered collection of :class:`TextDoc <textacy.texts.TextDoc>` s, all of
    the same language and sharing a single spaCy pipeline and vocabulary. Tracks
    overall corpus statistics and provides a convenient interface to alternate
    corpus representations.

    Args:
        lang (str): 2-letter code of corpus' language, used to initialize spaCy;
            see https://cloud.google.com/translate/v2/using_rest#language-params

    Add texts to corpus one-by-one with :meth:`TextCorpus.add_text() <textacy.texts.TextCorpus.add_text>`,
    or all at once with :meth:`TextCorpus.from_texts() <textacy.texts.TextCorpus.from_texts>`.
    Can also add already-instantiated TextDocs via :meth:`TextCorpus.add_doc() <textacy.texts.TextCorpus.add_doc>`.

    Iterate over corpus docs with ``for doc in TextCorpus``. Access individual docs
    by index (e.g. ``TextCorpus[0]`` or ``TextCorpus[0:10]``) or by boolean condition
    specified by a function (e.g. ``TextCorpus.get_docs(lambda x: len(x) > 100)``).
    """
    def __init__(self, lang):
        self.lang = lang
        self.spacy_pipeline = data.load_spacy(self.lang)
        self.spacy_vocab = self.spacy_pipeline.vocab
        self.spacy_stringstore = self.spacy_vocab.strings
        self.docs = []
        self.n_docs = 0
        self.n_sents = 0
        self.n_tokens = 0

    def __repr__(self):
        return 'TextCorpus({} docs; {} tokens)'.format(self.n_docs, self.n_tokens)

    def __len__(self):
        return self.n_docs

    def __getitem__(self, index):
        return self.docs[index]

    def __iter__(self):
        for doc in self.docs:
            yield doc

    @classmethod
    def from_texts(cls, lang, texts, metadata=None, n_threads=2, batch_size=1000):
        """
        Convenience function for creating a :class:`TextCorpus <textacy.texts.TextCorpus>`
        from an iterable of text strings.

        Args:
            lang (str)
            texts (iterable(str))
            metadata (iterable(dict), optional)
            n_threads (int, optional)
            batch_size (int, optional)

        Returns:
            :class:`TextCorpus <textacy.texts.TextCorpus>`
        """
        textcorpus = cls(lang=lang)
        spacy_docs = textcorpus.spacy_pipeline.pipe(
            texts, n_threads=n_threads, batch_size=batch_size)
        if metadata is not None:
            for spacy_doc, md in zip(spacy_docs, metadata):
                textcorpus.add_doc(TextDoc(spacy_doc, lang=lang,
                                           spacy_pipeline=textcorpus.spacy_pipeline,
                                           metadata=md))
        else:
            for spacy_doc in spacy_docs:
                textcorpus.add_doc(TextDoc(spacy_doc, lang=lang,
                                           spacy_pipeline=textcorpus.spacy_pipeline,
                                           metadata=None))
        return textcorpus

    def add_text(self, text, lang=None, metadata=None):
        """
        Create a :class:`TextDoc <textacy.texts.TextDoc>` from ``text`` and ``metadata``,
        then add it to the corpus.

        Args:
            text (str): raw text document to add to corpus as newly instantiated
                :class:`TextDoc <textacy.texts.TextDoc>`
            lang (str, optional):
            metadata (dict, optional): dictionary of document metadata, such as::

                {"title": "My Great Doc", "author": "Burton DeWilde"}

                NOTE: may be useful for retrieval via :func:`get_docs() <textacy.texts.TextCorpus.get_docs>`,
                e.g. ``TextCorpus.get_docs(lambda x: x.metadata["title"] == "My Great Doc")``
        """
        doc = TextDoc(text, spacy_pipeline=self.spacy_pipeline,
                      lang=lang, metadata=metadata)
        doc.corpus_index = self.n_docs
        doc.corpus = self
        self.docs.append(doc)
        self.n_docs += 1
        self.n_sents += doc.n_sents
        self.n_tokens += doc.n_tokens

    def add_doc(self, doc, print_warning=True):
        """
        Add an existing :class:`TextDoc <textacy.texts.TextDoc>` to the corpus as-is.
        NB: If ``textdoc`` is already added to this or another :class:`TextCorpus <textacy.texts.TextCorpus>`,
        a warning message will be printed and the ``corpus_index`` attribute will be
        overwritten, but you won't be prevented from adding the doc.

        Args:
            doc (:class:`TextDoc <textacy.texts.TextDoc>`)
            print_warning (bool, optional): if True, print a warning message if
                ``doc`` already added to a corpus; otherwise, don't ever print
                the warning and live dangerously
        """
        if doc.lang != self.lang:
            msg = 'TextDoc.lang {} != TextCorpus.lang {}'.format(doc.lang, self.lang)
            raise ValueError(msg)
        if hasattr(doc, 'corpus_index'):
            doc = copy.deepcopy(doc)
            if print_warning is True:
                print('**WARNING: TextDoc already associated with a TextCorpus; adding anyway...')
        doc.corpus_index = self.n_docs
        doc.corpus = self
        self.docs.append(doc)
        self.n_docs += 1
        self.n_sents += doc.n_sents
        self.n_tokens += doc.n_tokens

    def get_doc(self, index):
        """
        Get a single doc by its position ``index`` in the corpus.
        """
        return self.docs[index]

    def get_docs(self, match_condition, limit=None):
        """
        Iterate over all docs in corpus and return all (or N = ``limit``) for which
        ``match_condition(doc) is True``.

        Args:
            match_condition (func): function that operates on a :class:`TextDoc`
                and returns a boolean value; e.g. `lambda x: len(x) > 100` matches
                all docs with more than 100 tokens
            limit (int, optional): if not `None`, maximum number of matched docs
                to return

        Yields:
            :class:`TextDoc <textacy.texts.TextDoc>`: one per doc passing ``match_condition``
                up to ``limit`` docs
        """
        if limit is None:
            for doc in self:
                if match_condition(doc) is True:
                    yield doc
        else:
            n_matched_docs = 0
            for doc in self:
                if match_condition(doc) is True:
                    n_matched_docs += 1
                    if n_matched_docs > limit:
                        break
                    yield doc

    def remove_doc(self, index):
        """Remove the document at ``index`` from the corpus, and decrement the
        ``corpus_index`` attribute on all docs that come after it in the corpus."""
        for doc in self[index + 1:]:
            doc.corpus_index -= 1
        del self[index]

    def remove_docs(self, match_condition, limit=None):
        """
        Remove all (or N = ``limit``) docs in corpus for which ``match_condition(doc) is True``.
        Re-set all remaining docs' ``corpus_index`` attributes at the end.

        Args:
            match_condition (func): function that operates on a :class:`TextDoc <textacy.texts.TextDoc>`
                and returns a boolean value; e.g. ``lambda x: len(x) > 100`` matches
                all docs with more than 100 tokens
            limit (int, optional): if not None, maximum number of matched docs
                to remove
        """
        remove_indexes = [doc.corpus_index
                          for doc in self.get_docs(match_condition, limit=limit)]
        for index in remove_indexes:
            del self[index]
        # now let's re-set the `corpus_index` attribute for all docs at once
        for i, doc in enumerate(self):
            doc.corpus_index = i

    def as_doc_term_matrix(self, terms_lists, weighting='tf',
                           normalize=True, smooth_idf=True, sublinear_tf=False,
                           min_df=1, max_df=1.0, min_ic=0.0, max_n_terms=None):
        """
        Transform corpus into a sparse CSR matrix, where each row i corresponds
        to a doc, each column j corresponds to a unique term, and matrix values
        (i, j) correspond to the tf or tf-idf weighting of term j in doc i.

        .. seealso:: :func:`build_doc_term_matrix <textacy.representations.vsm.build_doc_term_matrix>`
        """
        self.doc_term_matrix, self.id_to_term = vsm.build_doc_term_matrix(
            terms_lists, weighting=weighting,
            normalize=normalize, smooth_idf=smooth_idf, sublinear_tf=sublinear_tf,
            min_df=min_df, max_df=max_df, min_ic=min_ic,
            max_n_terms=max_n_terms)
        return (self.doc_term_matrix, self.id_to_term)
