"""Official inference endpoints and offline, provider-reported model metadata."""
import copy
import json
from pathlib import Path

API_PROVIDERS = {
    'deepseek': {'name':'DeepSeek', 'env_key':'DEEPSEEK_API_KEY',
                 'base_url':'https://api.deepseek.com'},
    'opencode_go': {'name':'OpenCode Go', 'env_key':'OPENCODE_API_KEY',
                    'base_url':'https://opencode.ai/inference/go/openai/v1'},
}
CODE_ROOT = Path(__file__).resolve().parents[1]


def go_metadata(root):
    path = Path(root)/'config/opencode-go-models.json'
    if not path.exists():
        path = CODE_ROOT/'config/opencode-go-models.json'
    return json.loads(path.read_text(encoding='utf-8'))['models']


def go_models(root):
    return [{'id':m['id'], 'name':m['name'],
             'efforts':m['capabilities'].get('reasoning',{}).get('effort') or ['default'],
             'supports_images':any(x.startswith('image/') for x in m['modalities']['input']),
             'service_tiers':['default']}
            for m in go_metadata(root)]


def write_go_cli_catalog(root, run_dir):
    # Codex needs its own catalog shape. Limits and efforts come from the
    # published Go metadata; remaining fields are existing CLI schema defaults.
    base = json.loads((CODE_ROOT/'config/deepseek-models.json').read_text(encoding='utf-8'))['models'][0]
    models = []
    for m in go_metadata(root):
        value = copy.deepcopy(base)
        levels = m['capabilities'].get('reasoning',{}).get('effort') or ['high']
        context = m['limit'].get('input',m['limit']['context'])
        value.update(slug=m['id'], display_name=m['name'], description='OpenCode Go model',
                     context_window=context, max_context_window=context,
                     auto_compact_token_limit=int(context*.85),
                     input_modalities=['text']+(['image'] if any(x.startswith('image/') for x in m['modalities']['input']) else []),
                     supported_reasoning_levels=[{'effort':e,'description':e} for e in levels],
                     default_reasoning_level='high' if 'high' in levels else levels[0],
                     supports_reasoning_summaries=False)
        models.append(value)
    path = Path(run_dir)/'opencode-go-models.json'
    path.write_text(json.dumps({'models':models}),encoding='utf-8')
    return path
