"""File-backed candidate/nutrient adapters for controlled production-logic experiments.

No clients, data imports or production monkeypatches are installed at module import.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import math

import numpy as np

COLLECTIONS = {s: 'nutritional_ingredients_'+s for s in ['irish', 'hungarian', 'eu', 'slovenian']}


def native_candidate_id(candidate: dict, source: str) -> str:
    metadata = candidate.get('metadata') or {}
    field = 'canonical_food_id' if source in {'irish', 'hungarian'} else 'eu_id' if source == 'eu' else 'slovenian_id'
    return str(metadata.get(field) or candidate['id'])


def lucene_length(length: int) -> int:
    """Decode Lucene's byte4 length norm; short ingredient labels are lossless."""
    if length < 24:
        return length
    value = length - 24
    bits = value.bit_length()
    if bits < 4:
        encoded = value
    else:
        shift = bits - 4
        encoded = ((value >> shift) & 7) | ((shift + 1) << 3)
    shift = (encoded >> 3) - 1
    return 24 + ((encoded & 7) if shift < 0 else ((encoded & 7) | 8) << shift)


def phrase_frequency(tokens: list[list], query: list[list]) -> int:
    if not query:
        return 0
    positions = {position: word for word, position in tokens}
    first = query[0][1]
    return sum(all(positions.get(start + position - first) == word for word, position in query)
               for start, word in positions.items() if word == query[0][0])


class FrozenProductionCatalogue:
    """Single-shard frozen vector streams + standard BM25/phrase/RRF replay.

    Analyzer tokens come from the frozen Elasticsearch field analyzer. Retrieval
    parity is measured against captured responses before reporting catalogue effects.
    Added vectors use source-vector cosine; last-bit engine normalization differences
    can still affect ties involving new records.
    """
    def __init__(self, documents: list[dict], candidates: list[dict], vectors, queries: dict,
                 nutrient_rows: list[dict], *, pool_size: int = 25, rank_constant: int = 60,
                 statistics: dict | None = None, captured_vectors: list[dict] | None = None,
                 baseline_candidates: int | None = None):
        self.documents, self.candidates, self.queries = documents, candidates, queries
        self.pool_size, self.rank_constant = pool_size, rank_constant
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(candidates) or not np.isfinite(matrix).all():
            raise ValueError('Candidate/vector inventory mismatch')
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError('Zero stored vector')
        self.raw_vectors = matrix
        self.vectors = matrix / norms
        self.baseline_candidates = len(candidates) if baseline_candidates is None else baseline_candidates
        self.captured_vectors = {(r['collection'], r['query']): r['hits'] for r in captured_vectors or []}
        by_es_id = {r['es_id']: r for r in documents}
        if len(by_es_id) != len(documents):
            raise ValueError('Duplicate search document ID')
        df, total, count = Counter(), 0, 0
        for row in documents:
            tokens = row['tokens']
            if tokens:
                df.update({word for word, _ in tokens})
                total += len(tokens)
                count += 1
        self.df, self.doc_count, self.avg_length = df, count, total/max(count, 1)
        if statistics is not None:
            if not 0 <= statistics['live_document_count'] <= len(documents):
                raise ValueError('Statistics do not describe the frozen inventory')
            query_terms={word for q in queries.values() for word,_ in q['tokens']}
            if query_terms - statistics['doc_freq'].keys():
                raise ValueError('Missing frozen query term statistics')
            added = documents[statistics['live_document_count']:]
            self.df = Counter(statistics['doc_freq'])
            for row in added:
                self.df.update({word for word, _ in row['tokens']})
            self.doc_count = statistics['doc_count'] + sum(bool(r['tokens']) for r in added)
            self.avg_length = (statistics['sum_ttf'] + sum(len(r['tokens']) for r in added))/self.doc_count
        self.tokens = [by_es_id[c['es_id']]['tokens'] for c in candidates]
        self.lengths = np.asarray([lucene_length(len(t)) for t in self.tokens], dtype=float)
        self.postings = defaultdict(list)
        self.collections, self.ids = defaultdict(list), {}
        for index, (candidate, tokens) in enumerate(zip(candidates, self.tokens, strict=True)):
            self.collections[candidate['collection']].append(index)
            for word, frequency in Counter(word for word, _ in tokens).items():
                self.postings[word].append((index, frequency))
            self.ids.setdefault((candidate['collection'], str(candidate['id'])), index)
            source = next(s for s,c in COLLECTIONS.items() if c == candidate['collection'])
            self.ids.setdefault((candidate['collection'], native_candidate_id(candidate, source)), index)
        self.nutrients = {}
        for row in nutrient_rows:
            key = (row['source_key'], row['native_id'])
            if key in self.nutrients:
                raise ValueError('Duplicate nutrient key')
            self.nutrients[key] = row['record']
        self.cache, self.errors, self.calls = {}, [], []

    def by_id(self, collection: str, identifier: str) -> dict | None:
        index = self.ids.get((collection, str(identifier)))
        if index is None:
            return None
        return {k: copy.deepcopy(self.candidates[index][k]) for k in ['id', 'document', 'metadata']}

    def nutrient(self, source: str, identifier: str) -> dict | None:
        return copy.deepcopy(self.nutrients.get((source, str(identifier))))

    def query(self, collection: str, query: str) -> list[dict]:
        self.calls.append((collection, query))
        if (collection, query) in self.cache:
            return copy.deepcopy(self.cache[collection, query])
        if query not in self.queries:
            self.errors.append({'collection': collection, 'query': query, 'reason': 'uncaptured_query'})
            raise ValueError('Uncaptured query; offline retrieval must not silently use a new analyzer/model')
        q = self.queries[query]
        vector = np.asarray(q['embedding'], dtype=np.float32)
        if vector.ndim != 1 or vector.shape[0] != self.vectors.shape[1] or not np.isfinite(vector).all() or not np.linalg.norm(vector):
            self.errors.append({'collection':collection,'query':query,'reason':'invalid_query_vector'})
            raise ValueError('Invalid query embedding')
        indexes = np.asarray(self.collections.get(collection, []), dtype=int)
        if not len(indexes):
            return []
        similarity = self.vectors[indexes] @ (vector / np.linalg.norm(vector))
        scores = dict(zip(indexes.tolist(), similarity.tolist()))
        reference = self.captured_vectors.get((collection, query))
        if self.captured_vectors and reference is None:
            self.errors.append({'collection':collection, 'query':query, 'reason':'uncaptured_vector_stream'})
            raise ValueError('Missing frozen vector stream')
        if reference is None:
            vector_pool = indexes[np.argsort(-similarity, kind='stable')[:self.pool_size]].tolist()
        else:
            # Indexed vectors are normalized internally; source vectors alone lose
            # the last bits that production uses to break cross-source ties.
            old = sorted((h for h in reference if 'vector_rank' in h), key=lambda h:h['vector_rank'])
            old_indexes = [self.ids[collection, str(h['id'])] for h in old]
            for index, hit in zip(old_indexes, old, strict=True):
                scores[index] = 1-hit['distance']
            additions = [i for i in indexes if i >= self.baseline_candidates]
            vector_pool = sorted(old_indexes + additions, key=lambda i:-scores[i])[:self.pool_size]
        lexical = np.zeros(len(self.candidates))
        norm = np.float32(1.2) * (np.float32(.25) + np.float32(.75)*self.lengths.astype(np.float32)/np.float32(max(self.avg_length, 1)))
        idfs = {word: np.float32(math.log(1 + (self.doc_count-self.df[word]+.5)/(self.df[word]+.5)))
                for word, _ in q['tokens']}
        for word, frequency in Counter(word for word, _ in q['tokens']).items():
            for index, tf in self.postings.get(word, []):
                weight = np.float32(2.2) * idfs[word]
                lexical[index] += frequency * np.float32(weight-weight/(np.float32(1)+np.float32(tf)/norm[index]))
        for index in indexes:
            if lexical[index] > 0:
                tf = phrase_frequency(self.tokens[index], q['tokens'])
                if tf:
                    weight = np.float32(4.4) * np.float32(sum(float(idfs[word]) for word,_ in q['tokens']))
                    lexical[index] += np.float32(weight-weight/(np.float32(1)+np.float32(tf)/norm[index]))
        lexical = lexical.astype(np.float32)
        positive = indexes[lexical[indexes] > 0]
        lexical_pool = positive[np.argsort(-lexical[positive], kind='stable')[:self.pool_size]].tolist()
        merged = {}
        for rank, index in enumerate(vector_pool, 1):
            merged[index] = {'vector_rank': rank}
        for rank, index in enumerate(lexical_pool, 1):
            merged.setdefault(index, {}).update(lexical_rank=rank, lexical_score=float(lexical[index]))
        result = []
        for index, ranks in merged.items():
            candidate = self.candidates[index]
            distance = float(max(0, min(2, 1-scores[index])))
            if 'vector_rank' not in ranks:
                left, right = vector.tolist(), self.raw_vectors[index].tolist()
                distance = max(0, min(2, 1-sum(a*b for a,b in zip(left,right,strict=True))/
                    math.sqrt(sum(a*a for a in left)*sum(b*b for b in right))))
            result.append({k: copy.deepcopy(candidate[k]) for k in ['id', 'document', 'metadata']} | ranks | {
                'distance': distance,
                'rrf_score': sum(1/(self.rank_constant+ranks[k]) for k in ['vector_rank', 'lexical_rank'] if k in ranks)})
        result.sort(key=lambda r: (-r['rrf_score'], r['distance']))
        self.cache[collection, query] = result
        return copy.deepcopy(result)
