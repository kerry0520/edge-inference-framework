"""Read-only frozen task scoring; no device action, inference, or rubric adaptation."""
import argparse
import json
from pathlib import Path


def json_equal(actual, expected):
    # bool is an int subclass in Python; preserve JSON value types recursively.
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(json_equal(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(json_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def answer_match(text, rule):
    if not isinstance(text, str):
        return False
    value = text.strip()
    if rule['kind'] == 'json_object':
        try:
            # Reject duplicate keys rather than silently retaining the last value.
            def pairs(items):
                result = {}
                for key, item in items:
                    if key in result:
                        raise ValueError('duplicate JSON key')
                    result[key] = item
                return result
            actual = json.loads(value, object_pairs_hook=pairs,
                                parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        except (ValueError, TypeError):
            return False
        return json_equal(actual, rule['expected'])
    if rule['kind'] == 'literal' and rule.get('allow_terminal_punctuation'):
        if value and value[-1] in '.。!！?？':
            value = value[:-1].rstrip()
    return value == rule['expected']


def score_case(case, rule, result):
    count = result.get('sample_count')
    ids = result.get('output_ids')
    inputs = result.get('full_input_ids')
    status = result.get('status')
    budget = result.get('budget')
    valid = (result.get('case_id') == case['case_id'] and type(budget) is int
             and budget == case['budget'] and type(count) is int and 0 < count <= budget
             and isinstance(result.get('text'), str)
             and isinstance(ids, list) and len(ids) == count and all(type(n) is int and 0 <= n < 151936 for n in ids)
             and isinstance(inputs, list) and bool(inputs) and all(type(n) is int and 0 <= n < 151936 for n in inputs)
             and type(status) is int and status in (1, 2)
             and (status != 2 or count == budget)
             and type(result.get('exit_code')) is int and result['exit_code'] == 0)
    matched = answer_match(result.get('text'), rule)
    completion = 'EOS' if status == 1 else 'MAX_TOKENS' if status == 2 else 'ERROR_OR_UNKNOWN'
    return {'case_id': case['case_id'], 'category': case['category'],
            'answer_match': matched, 'completion': completion, 'metadata_valid': valid,
            'accepted': bool(valid and matched and status == 1),
            'empty_output': isinstance(result.get('text'), str) and not bool(result['text'].strip())}


def score(fixture, rubric, results):
    if fixture['fixture_id'] != rubric['fixture_id']:
        raise ValueError('fixture/rubric identity differs')
    rows = results['results'] if isinstance(results, dict) else results
    by_id = {}
    for row in rows:
        identifier = row['case_id']
        if identifier in by_id:
            raise ValueError('duplicate result case_id: ' + identifier)
        by_id[identifier] = row
    expected = {c['case_id'] for c in fixture['cases']}
    if set(by_id) != expected:
        raise ValueError('missing or unexpected result case IDs')
    scored = [score_case(c, rubric['rules'][c['case_id']], by_id[c['case_id']]) for c in fixture['cases']]
    return {'fixture_id': fixture['fixture_id'], 'case_count': len(scored),
            'accepted_count': sum(r['accepted'] for r in scored),
            'answer_match_count': sum(r['answer_match'] for r in scored),
            'max_tokens_count': sum(r['completion'] == 'MAX_TOKENS' for r in scored),
            'invalid_metadata_count': sum(not r['metadata_valid'] for r in scored),
            'cases': scored,
            'scope': 'Local fixed tasks, not a standard benchmark, quality-equivalence proof, or high-precision gold.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--rubric', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    args = parser.parse_args()
    def read(path):
        return json.loads(path.read_text(encoding='utf-8-sig'))
    print(json.dumps(score(read(args.fixture), read(args.rubric), read(args.results)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
