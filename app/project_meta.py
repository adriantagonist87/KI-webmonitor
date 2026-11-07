"""Validated project appearance and user-defined tags; independent of monitoring."""
import json
import re
import unicodedata

DEFAULT_COLOR='#1967d2'


def normalize_color(value):
    value=str(value).strip().lower()
    if not re.fullmatch(r'#[0-9a-f]{6}',value):raise ValueError('Bitte eine Farbe im Format #RRGGBB angeben.')
    return value


def normalize_tags(value):
    if isinstance(value,str):
        if len(value)>700:raise ValueError('Tags sind zu lang.')
        value=re.split(r'[,;\n]',value)
    if not isinstance(value,list):raise ValueError('Tags als Liste oder kommagetrennten Text angeben.')
    result=[];seen=set()
    for raw in value:
        if not isinstance(raw,str):raise ValueError('Tags muessen Text sein.')
        tag=' '.join(unicodedata.normalize('NFKC',raw).split())
        if not tag:continue
        if len(tag)>40:raise ValueError('Ein Tag darf maximal 40 Zeichen enthalten.')
        if tag.casefold() not in seen:result.append(tag);seen.add(tag.casefold())
    if len(result)>12:raise ValueError('Maximal 12 Tags pro Projekt.')
    return result


def appearance(row):
    try:color=normalize_color(row.get('color',DEFAULT_COLOR))
    except ValueError:color=DEFAULT_COLOR
    try:tags=normalize_tags(json.loads(row.get('tags','[]')) if isinstance(row.get('tags','[]'),str) else row['tags'])
    except (ValueError,TypeError,KeyError):tags=[]
    return {'color':color,'tags':tags,'tag_keys':[t.casefold() for t in tags]}
