import json, sys
from pathlib import Path
from jsonschema import Draft202012Validator,FormatChecker
ROOT=Path(__file__).resolve().parents[2]

def test_example_schemas():
    for kind,schemaname in [('context.updated','vehicle-context.schema.json'),('dj.speak','broadcast-command.schema.json')]:
        data=json.loads((ROOT/'examples'/f'{kind}.json').read_text())
        schema=json.loads((ROOT/'contracts'/'schemas'/schemaname).read_text())
        Draft202012Validator(schema,format_checker=FormatChecker()).validate(data)

def test_contract_definitions_are_v1():
    assert json.loads((ROOT/'examples'/'context.updated.json').read_text())['schema_version']=='1.0'
