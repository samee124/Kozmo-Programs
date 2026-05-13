# Normalization Specification

## Location
src/Cobalt/intake/normalizer.py

## Functions

def detect_country_hint(cleaned: str) -> str | None
def normalize(cleaned: str, country_code: str | None = None) -> tuple[str, str]
  Returns (normalized_name, comparison_key)

## Transformation Pipeline (comparison_key)
Step 1: Lowercase
Step 2: Collapse abbreviation dots (I.B.M. → ibm)
Step 3: Strip legal suffixes (country-aware, longest first)
Step 4: Remove & entirely (AT&T → att)
Step 5: Remove punctuation except hyphens between word chars
Step 6: Collapse spaces (all-short tokens → remove spaces)
Step 7: Strip whitespace

## Country Suffix Tables
Anglo: inc, corp, ltd, limited, llc, llp, plc, co, company,
       group, holdings, international, intl, services, solutions,
       technologies, systems, consulting, partners, associates
DE: gmbh, ag, kg, ohg, ug, kgaa
FR: sarl, sas, sa, eurl, sci, snc
IN: pvt ltd, pvt, private limited, public limited, limited
AU: pty ltd, pty limited, pty
SG: pte ltd, pte limited, pte
BR: ltda, sa, eireli
NL: bv, nv, vof, cv
AE: fze, fzc, llc
JP: 株式会社, 有限会社, 合同会社

## Country Detection (detect_country_hint)
AG/GmbH/KG suffix → DE
SARL/SAS → FR
株式会社/有限会社 → JP
Pvt Ltd/Private Limited → IN
Pty Ltd → AU
Pte Ltd → SG
Ltda → BR
FZE/FZC → AE
None otherwise

## Two-Tier Suffix Strip
Tier 1 (legal entity designators): always strip
Tier 2 (descriptors): only strip if tier-1 was also stripped
Prevents "Amazon Web Services" → "amazon" (services is tier-2 only)
Allows "Aramark Corp Services" → "aramark" (corp is tier-1, services follows)

## Known Expected Outputs
"IBM" → key="ibm"
"I.B.M." → key="ibm"
"I B M" → key="ibm"
"Infosys Ltd" → key="infosys"
"Infosys Limited" → key="infosys"
"Siemens AG" (DE) → key="siemens"
"Aramark Services" → key="aramark services"
"Amazon Web Services" → key="amazon web services"
"AT&T" → key="att"
"Coca-Cola" → key="coca-cola"
"ARAMARK CORP" → key="aramark"
