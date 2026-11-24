import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def normalize(value):
    return "".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


@dataclass(frozen=True)
class Location:
    name: str
    region: str
    evidence: str


MUNICIPALITIES = json.loads(Path(__file__).with_name("municipalities.json").read_text(encoding="utf-8"))
CITIES = {normalize(city["name"]): city for city in MUNICIPALITIES}
CITY = "(?:" + "|".join(map(re.escape, sorted(CITIES, key=len, reverse=True))) + ")"
CITY_LOC = re.compile(
    rf"(?:\b(?:em|no municipio de|na cidade de|na regiao de|entre)\s+|^)"
    rf"(?P<city>{CITY})(?![\w-])"
)
CITY_UF = re.compile(rf"\b(?P<city>{CITY})(?:\s*[,(/-]\s*| +)(?:pr\b|parana\b)")
STATE = re.compile(
    r"\b(?:no|pelo|do estado do|estado do|governo do|regiao do|litoral do|norte do|"
    r"sul do|oeste do|interior do|municipios do|cidades do)\s+parana\b|"
    r"^parana(?:\s+(?:tem|recebe|registra|abre|anuncia|confirma|lanca|lidera|ganha|"
    r"tera|vai|amplia|aprova|bate|chega|e)\b|[,:])|"
    r"\b(?:municipios|cidades|rodovias|escolas|prefeituras|moradores|familias|alunos|"
    r"produtores|paranaenses|empresas)\s+(?:paranaenses|do parana)\b"
)
OTHER_STATES = (
    "acre|alagoas|amapa|amazonas|bahia|ceara|distrito federal|espirito santo|goias|"
    "maranhao|mato grosso(?: do sul)?|minas gerais|para|paraiba|pernambuco|piaui|"
    "rio de janeiro|rio grande do (?:norte|sul)|rondonia|roraima|santa catarina|"
    "sao paulo|sergipe|tocantins|argentina|paraguai|uruguai|chile|estados unidos|brasilia|"
    "sao paulo|florianopolis|porto alegre|belo horizonte|sao luis|salvador|fortaleza|recife|manaus"
)
OUTSIDE = re.compile(
    rf"\b(?:em|no estado d[eo]|na cidade de|na capital d[eo]|acontece[ur]? em|ocorre[ur]? em)\s+"
    rf"(?:{OTHER_STATES})\b(?!\s+(?:do|da|dos|das)\b)|"
    r"(?:\(|/|,\s*)(?:AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PE|PI|RJ|RN|RS|RO|"
    r"RR|SC|SP|SE|TO)(?:\)|\b)",
    re.IGNORECASE,
)
MENTION_ONLY = re.compile(
    r"\b(?:parana clube|rio parana|universidade federal do parana|time do parana|"
    r"equipe do parana|natural do parana|nascid[oa] no parana)\b"
)
AMBIGUOUS_WORDS = {
    "alto parana", "balsa nova", "campina grande do sul", "flora", "cidade gaucha",
    "cidade", "reserva", "realeza", "santa fe", "salto", "entre rios do oeste",
    "primeiro de maio", "quatro pontes", "perola", "planalto", "ipiranga", "palmas",
    "maringa", "bandeirantes", "imbituva", "morretes", "paraiso do norte", "porto rico",
    "diamante do norte", "formosa do oeste", "floresta", "engenheiro beltrao",
    "douradina", "terra boa", "santa mariana", "bom sucesso", "bom jesus do sul",
    "sao jorge do oeste", "santa tereza do oeste", "terra rica", "brasilandia do sul",
    "presidente castelo branco", "campo largo", "doutor camargo", "doutor ulysses",
    "luz", "bela vista do paraiso", "lindoeste", "pato branco", "general carneiro",
    "sao pedro do parana", "sao joao do caiua", "tunapolis",
}
# Cidades com identidade regional clara podem ser reconhecidas no título sem "PR".
CLEAR_NAMES = {"curitiba", "foz do iguacu", "londrina", "maringa", "ponta grossa", "pato branco"}


def classify(title, summary):
    headline = normalize(title)
    # Um local externo no título exige análise editorial fora do radar automático.
    if OUTSIDE.search(title) or OUTSIDE.search(headline):
        return None

    leads = [(title, headline)]
    for sentence in re.split(r"(?<=[!?]|\.)\s+", summary.strip(), maxsplit=2)[:2]:
        if sentence:
            leads.append((sentence, normalize(sentence)))

    for original, content in leads:
        if OUTSIDE.search(original) or OUTSIDE.search(content):
            continue
        for match in CITY_UF.finditer(content):
            city = CITIES[match["city"]]
            return Location(city["name"], city["region"], original[:500])
        for match in CITY_LOC.finditer(content):
            key = match["city"]
            city = CITIES[key]
            if key in CLEAR_NAMES or (
                not city["ambiguous"] and key not in AMBIGUOUS_WORDS
                and (match.start() > 0 or re.search(r"^[:,-]|\s(?:registra|recebe|tem|anuncia|abre)\b",
                                                   content[match.end():match.end() + 24]))
            ):
                return Location(city["name"], city["region"], original[:500])
        if STATE.search(MENTION_ONLY.sub("", content)):
            return Location("Paraná", "Todo o Paraná", original[:500])
    return None

