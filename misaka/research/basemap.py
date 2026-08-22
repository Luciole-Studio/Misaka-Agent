"""Optional coverage maps for broad surveys.

The bundled OCM, CAP, and JEL top-level categories are prompts for overlooked dimensions, not a universal ontology
or source of truth. Each reflects its field, period, and institutional perspective. Use several maps when useful, expand
them only when the question warrants it, and let the research problem determine the final structure.
"""

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS basemap (
  id     TEXT PRIMARY KEY,
  scheme TEXT NOT NULL,
  code   TEXT NOT NULL,
  label  TEXT NOT NULL,
  note   TEXT
);
"""

# Top-level seeds from three established classification systems.
SEEDS = [
    # Broad OCM categories from the Outline of Cultural Materials (HRAF).
    ("OCM", "10", 'Location and geography', None), ("OCM", "12", 'Natural environment and resources', None),
    ("OCM", "14", 'Population', None), ("OCM", "16", 'Ethnic groups and identity', None),
    ("OCM", "19", 'Language', None), ("OCM", "20", 'Communication and media', None),
    ("OCM", "22", 'Food and subsistence', None), ("OCM", "26", 'Food consumption and ritual', None),
    ("OCM", "34", 'Structures and living arrangements', None), ("OCM", "36", 'Settlements and towns', None),
    ("OCM", "43", 'Exchange and trade', None), ("OCM", "46", 'Labour and division of labour', None),
    ("OCM", "47", 'Business and finance', None), ("OCM", "48", 'Transport', None),
    ("OCM", "55", 'Health and illness', None), ("OCM", "58", 'Marriage', None),
    ("OCM", "59", 'Kinship and households', None), ("OCM", "62", 'Community organization', None),
    ("OCM", "63", 'Territorial political organization', None), ("OCM", "67", 'Law and sanctions', None),
    ("OCM", "69", 'Conflict and war', None), ("OCM", "77", 'Religious beliefs and practices', None),
    ("OCM", "81", 'Knowledge and science', None), ("OCM", "87", 'Life cycle and education', None),
    # Major issue codes from the Comparative Agendas Project.
    ("CAP", "1", 'Macroeconomics', None), ("CAP", "2", 'Civil rights and minorities', None),
    ("CAP", "3", 'Health', None), ("CAP", "4", 'Agriculture', None),
    ("CAP", "5", 'Labour and employment', None), ("CAP", "6", 'Education', None),
    ("CAP", "7", 'Environment', None), ("CAP", "8", 'Energy', None),
    ("CAP", "9", 'Immigration', None), ("CAP", "10", 'Transport', None),
    ("CAP", "12", 'Law, crime, and family issues', None), ("CAP", "13", 'Social welfare', None),
    ("CAP", "14", 'Housing and urban development', None), ("CAP", "15", 'Domestic commerce and finance', None),
    ("CAP", "16", 'Defence', None), ("CAP", "17", 'Science, technology, and communications', None),
    ("CAP", "18", 'Foreign trade', None), ("CAP", "19", 'International affairs and foreign assistance', None),
    ("CAP", "20", 'Government operations', None), ("CAP", "21", 'Public land and water resources', None),
    ("CAP", "23", 'Cultural policy', None),
    # Top-level Journal of Economic Literature classification codes.
    ("JEL", "A", 'General economics and teaching', None), ("JEL", "B", 'History and methodology of economic thought', None),
    ("JEL", "C", 'Mathematical and quantitative methods', None), ("JEL", "D", 'Microeconomics', None),
    ("JEL", "E", 'Macro and monetary economics', None), ("JEL", "F", 'International economics', None),
    ("JEL", "G", 'Financial economics', None), ("JEL", "H", 'Public economics', None),
    ("JEL", "I", 'Health, education, and welfare', None), ("JEL", "J", 'Labour and demographic economics', None),
    ("JEL", "K", 'Law and economics', None), ("JEL", "L", 'Industrial organization', None),
    ("JEL", "M", 'Business administration and business economics', None), ("JEL", "N", 'Economic history', None),
    ("JEL", "O", 'Economic development and technological change', None), ("JEL", "P", 'Economic systems', None),
    ("JEL", "Q", 'Agricultural, environmental, and natural-resource economics', None), ("JEL", "R", 'Urban and regional economics', None),
    ("JEL", "Y", 'Miscellaneous categories', None), ("JEL", "Z", 'Other special topics', None),
]


def path():
    return os.path.expanduser(os.environ.get("MISAKA_BASEMAP", "~/.misaka/basemap.db"))


def connect(p=None):
    p = p or path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    con = sqlite3.connect(p, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def load_seeds(con, seeds=None):
    "Load bundled seeds and return the number of inserted rows."
    n = 0
    for scheme, code, label, note in (seeds or SEEDS):
        cid = f"{scheme}-{code}"
        cur = con.execute(
            "INSERT OR IGNORE INTO basemap (id, scheme, code, label, note) VALUES (?,?,?,?,?)",
            (cid, scheme, code, label, note))
        n += cur.rowcount
    return n


def cells(con, schemes=None):
    q = "SELECT * FROM basemap"
    args = []
    if schemes:
        q += " WHERE scheme IN (%s)" % ",".join("?" * len(schemes))
        args = list(schemes)
    return con.execute(q + " ORDER BY scheme, CAST(code AS INTEGER), code", args).fetchall()


def stats(con):
    return con.execute("SELECT scheme, COUNT(*) n FROM basemap GROUP BY scheme").fetchall()


def survey_body(cells, proposition):
    """Build a coverage-screening card without asserting that the maps are complete."""
    listing = "\n".join(f"- [{c['id']}] {c['label']}" for c in cells)
    return f"""## goal
Create `survey.md` as a coverage-screening pass for this proposition:

{proposition}

For each category below, decide whether it has a plausible, material connection to the proposition. A category that
does not connect should not create more research work.

{listing}

## boundaries
Identify likely connections and name their mechanisms. Do not investigate or prove them in this card.
The classification maps are prompts, not evidence of completeness.

## acceptance criteria
- `survey.md` exists.
- All {len(cells)} categories receive one explicit `connects` or `does not connect` decision.
- Every `connects` decision names a concrete mechanism: who or what affects whom or what, and through which process.
- The report ends with a short list of uncertain borderline categories for possible follow-up.
"""
