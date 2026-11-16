"""Offline interpreter for the emitted exclusion DSL subset, not an ES emulator."""
from fnmatch import fnmatchcase

def value(doc,path):
    for key in path.split('.'):
        if key=='kw':
            if isinstance(doc,str):doc=doc.lower()
            continue
        if not isinstance(doc,dict):return None
        doc=doc.get(key)
    return doc

def matches(query,doc):
    if 'term' in query or 'terms' in query:
        key='term' if 'term' in query else 'terms'
        field,wanted=next(iter(query[key].items()))
        actual=value(doc,field)
        actual=actual if isinstance(actual,list) else [actual]
        wanted=wanted if isinstance(wanted,list) else [wanted]
        return any(v in wanted for v in actual if v is not None)
    if 'exists' in query:return value(doc,query['exists']['field']) is not None
    if 'wildcard' in query:
        field,options=next(iter(query['wildcard'].items()))
        actual=value(doc,field)
        if actual is None:return False
        pattern=options['value']
        if options.get('case_insensitive'):actual=actual.lower();pattern=pattern.lower()
        return fnmatchcase(actual,pattern)
    if 'nested' in query:
        nested=query['nested'];rows=value(doc,nested['path']) or []
        return any(matches(nested['query'],{nested['path']:row}) for row in rows)
    if 'bool' in query:
        body=query['bool']
        def clauses(name):
            raw=body.get(name,[])
            return [raw] if isinstance(raw,dict) else raw
        if not all(matches(q,doc) for q in clauses('must')+clauses('filter')):return False
        if any(matches(q,doc) for q in clauses('must_not')):return False
        minimum=body.get('minimum_should_match',0 if clauses('must') or clauses('filter') else int(bool(clauses('should'))))
        return sum(matches(q,doc) for q in clauses('should'))>=minimum
    raise ValueError('Unsupported offline query operator: '+str(list(query)))
