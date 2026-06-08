"""One-off demo: conservative discovery-mode enrichment over products_normalized.json."""
import json
from collections import Counter, OrderedDict

SRC = r'C:/Users/User/Desktop/PersonaFirst-backend/products_normalized.json'
OUT = r'C:/Users/User/Desktop/PersonaFirst-backend/products_enriched.json'

with open(SRC, encoding='utf-8') as f:
    products = json.load(f)

ACTIVITY_RULES = [
    ('Yoga',     {'yoga'},                     0.90),
    ('Barre',    {'barre'},                    0.92),
    ('Cardio',   {'cardio'},                   0.85),
    ('Running',  {'run', 'running', 'runner'}, 0.82),
    ('Training', {'train'},                    0.80),
    ('Tennis',   {'tennis'},                   0.85),
    ('Lounge',   {'lounge'},                   0.80),
]

INTENSITY_RULES = [
    ('High',   {'high impact', 'performance', 'cardio', 'run', 'running'}, 0.70),
    ('Medium', {'train', 'barre', 'yoga', 'tennis'},                       0.65),
    ('Low',    {'lounge', 'low impact'},                                   0.75),
]

ENVIRONMENT_RULES = [
    ('Studio',  {'yoga', 'barre', 'pilates'},       0.70),
    ('Gym',     {'train', 'performance', 'cardio'}, 0.60),
    ('Outdoor', {'run', 'running', 'tennis'},       0.55),
    ('Indoor',  {'lounge'},                         0.75),
]

LAYERING_RULES = [
    ('Base Layer',  {'bra', 'bras', 'bras & tops', 'tank', 'tank tops',
                     'tee', 'henley', 'polo', 'bodysuit', 'bodysuits',
                     'onesie', 'dress', 'dresses', 'onesies & dresses'}, 0.85),
    ('Mid Layer',   {'sweatshirt', 'sweatshirts & hoodies', 'hoodie',
                     'cardigan', 'sweater', 'long sleeves', 'long sleeve'}, 0.78),
    ('Outer Layer', {'outerwear', 'jacket', 'jacket & coverups',
                     'jackets & coverups', "men s outerwear", "women s outerwear"}, 0.88),
]


def signal_tokens(p):
    toks = set()
    for part in p.get('clean_description', '').split(','):
        t = part.strip().lower()
        if t:
            toks.add(t)
    for c in p.get('functional_categories', []):
        toks.add(c.strip().lower())
    return toks


def match_any(tokens, wanted):
    hits = set()
    for w in wanted:
        if w in tokens:
            hits.add(w)
        else:
            for t in tokens:
                if w in t.split():
                    hits.add(t)
                    break
    return hits


def source_for(hits, p):
    cats_lower = {c.lower() for c in p.get('functional_categories', [])}
    return 'functional_categories' if any(h in cats_lower for h in hits) else 'clean_description'


def propose(p, rules):
    tokens = signal_tokens(p)
    out = []
    for value, triggers, conf in rules:
        hits = match_any(tokens, triggers)
        if not hits:
            continue
        out.append(OrderedDict([
            ('value', value),
            ('confidence', conf),
            ('evidence', f"{source_for(hits, p)} contains {sorted(hits)}"),
        ]))
    return out


def propose_layering(p):
    tokens = signal_tokens(p)
    for value, triggers, conf in reversed(LAYERING_RULES):  # outer first
        hits = match_any(tokens, triggers)
        if hits:
            return [OrderedDict([
                ('value', value),
                ('confidence', conf),
                ('evidence', f"{source_for(hits, p)} contains {sorted(hits)}"),
            ])]
    return []


enriched = []
for p in products:
    pv = OrderedDict([
        ('activity_type',     propose(p, ACTIVITY_RULES)),
        ('workout_intensity', propose(p, INTENSITY_RULES)),
        ('environment',       propose(p, ENVIRONMENT_RULES)),
        ('layering_role',     propose_layering(p)),
    ])
    enriched.append(OrderedDict([
        ('product_id', p['product_id']),
        ('style_code', p['style_code']),
        ('name', p['name']),
        ('clean_description', p['clean_description']),
        ('functional_categories', p['functional_categories']),
        ('proposed_values', pv),
    ]))

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(enriched, f, indent=2, ensure_ascii=False)

print(f"Enriched products: {len(enriched)}  ->  products_enriched.json")
print()
print('=== 10 SAMPLE ENRICHED PRODUCTS ===')
for e in enriched[:10]:
    print()
    print(f"-- {e['style_code']} | {e['name']}")
    print(f"   clean_description     : {e['clean_description']}")
    print(f"   functional_categories : {e['functional_categories']}")
    for attr in ['activity_type', 'workout_intensity', 'environment', 'layering_role']:
        props = e['proposed_values'][attr]
        if not props:
            print(f"   {attr:18s}    : (no confident signal - skipped)")
        else:
            for pr in props:
                print(f"   {attr:18s}    : {pr['value']}  (conf={pr['confidence']}; {pr['evidence']})")

print()
print('=== MOST COMMON PROPOSED VALUES PER ATTRIBUTE ===')
for attr in ['activity_type', 'workout_intensity', 'environment', 'layering_role']:
    c = Counter()
    none_count = 0
    for e in enriched:
        props = e['proposed_values'][attr]
        if not props:
            none_count += 1
        for pr in props:
            c[pr['value']] += 1
    print(f"\n{attr}:")
    for v, n in c.most_common():
        print(f"   {v:12s} -> {n}")
    print(f"   (no proposal) -> {none_count}")

print()
print('=== POTENTIAL NOISE / INCONSISTENCY ===')
issues = []
for e in enriched:
    acts = [pr['value'] for pr in e['proposed_values']['activity_type']]
    envs = [pr['value'] for pr in e['proposed_values']['environment']]
    lay  = [pr['value'] for pr in e['proposed_values']['layering_role']]
    if len(acts) >= 3:
        issues.append(f"{e['style_code']}: {len(acts)} activity_type proposals -> {acts}")
    if 'Outdoor' in envs and 'Studio' in envs:
        issues.append(f"{e['style_code']}: conflicting environment (Outdoor + Studio) -> {envs}")
    if not acts and not lay:
        issues.append(f"{e['style_code']}: no activity_type AND no layering_role -- {e['name']}")
if not issues:
    print('   (none detected)')
else:
    for i in issues:
        print('  -', i)
