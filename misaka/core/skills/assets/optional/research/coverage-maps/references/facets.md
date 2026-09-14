# Five facets

A subject is not a point in one tree but a combination of facets. Library science settled
this between the 1930s and 1950s (Ranganathan), and every large scheme since carries the
idea: UDC's auxiliary tables, FAST's nine facets, the Getty AAT's eight. A research planner
uses the trick in reverse: cut the question along each facet and see which cuts return
nothing; that is where a dimension was forgotten. Write one line per facet.

| Facet | Ask | Authority to consult |
|---|---|---|
| Discipline | Which fields discuss this, and which neighbouring field would frame it differently? | `disciplines.md` |
| Period | Boundaries of the span; does anything change inside it; whose periodisation? | PeriodO; FAST time periods; tables in section 2 |
| Place | Where, at what scale; which gazetteer names it; what did contemporaries call it? | Getty TGN; Pleiades; CHGIS; World Historical Gazetteer; FAST geographic |
| Source type / genre | Which kinds of evidence exist, and which have you not touched? | Remains vs tradition; Getty AAT; Iconclass; FAST form/genre; table in section 4 |
| Method | Which way of knowing is the plan using, and what would another yield? | SAGE Methods Map; ELSST; list in section 5 |
| Perspective, scale, language, medium, evidence quality, agency (editorial) | Section 6 | none: no library scheme has them |

## 1. Facet theory

### Ranganathan's PMEST (Colon Classification)
Source: https://www.isko.org/cyclo/colon_classification (accessed 2026-08-25)

S. R. Ranganathan's Colon Classification (1st ed. 1933) was the first fully faceted scheme:
instead of enumerating every compound subject it lists isolates in a few facets per class and
synthesises the class number with punctuation (the colon gave it its name). In the 4th
edition (1952) the many special facets (problem, institution, substance...) were abstracted
into five fundamental categories, Personality, Matter, Energy, Space, Time, "famously known
as PMEST"; citation order follows that formula, with "rounds and levels" when a category
recurs, and five remains the smallest set of categories proposed for any bibliographic
classification. For a planner: Personality is the thing itself (the kind, the who or what),
Matter its material or property, Energy the process, action or problem, Space the place,
Time the period. A plan whose tasks all sit on Personality has four facets unexamined.

### UDC common auxiliary tables
Source: https://udcsummary.info/php/index.php?lang=en and https://udcsummary.info/php/index.php?id=11791&lang=en (accessed 2026-08-25)

The Universal Decimal Classification keeps its main tables (0-9) for the discipline and lets
any *common auxiliary* be appended to any class, so one number can carry discipline, place,
time, language, form and persons at once. The auxiliary tables in the UDC Summary:

| Table | Notation | Facet |
|---|---|---|
| 1a, 1b | `+` `/` `:` `::` `[ ]` | coordination, consecutive extension, simple relation, order-fixing, subgrouping |
| 1c | `=...` | language |
| 1d | `(0...)` | form (of the document) |
| 1e | `(1/9)` | place |
| 1f | `(=...)` | human ancestry, ethnic grouping and nationality |
| 1g | `"..."` | time |
| 1h | `*`, `A/Z` | non-UDC notation; direct alphabetical specification |
| 1k | `-02` `-03` `-04` `-05` | general characteristics: properties; materials; relations, processes and operations; persons and personal characteristics |

Table 1k's `-05` (persons as agents `-051` vs targets `-052`; by age `-053`, ethnicity or
nationality `-054`, gender and kinship `-055`) and `-04` (phase relations, general processes)
are the closest a library scheme comes to "whose view" and "mechanism".

### FAST facets (OCLC)
Source: https://www.oclc.org/research/areas/data-science/fast.html (accessed 2026-08-25)

FAST (Faceted Application of Subject Terminology), derived from LCSH by OCLC Research with the
Library of Congress from 1998, splits every heading into *nine* facets usable together or
alone: Personal names, Corporate names, Meeting names, Geographic names, Events, Titles, Time
periods, Topics, Form/Genre. (Summaries that say eight omit Meeting names.) For a planner this
is the pragmatic checklist: who (persons, bodies, meetings), where, when, which event, which
work, which topic, which form.

## 2. Period facet

### PeriodO, the gazetteer of period definitions
Source: https://perio.do/ and https://perio.do/guide/ (accessed 2026-08-25)

PeriodO lives at perio.do (the older address periodo.github.io now returns 404). It is a
public-domain gazetteer of scholarly definitions of historical, art-historical and
archaeological periods, built so that datasets which define periods differently can be
linked, and so that overlaps and divergences become visible. Every period belongs to an
*authority* (the publication that defined it), carries label, temporal extent and spatial
coverage exactly as that authority gave them, and has a permanent ARK permalink of the form
`http://n2t.net/ark:/99152/p0...`; authorities have permalinks too; append `.json` or `.ttl`
for structured data. The whole dataset is at http://n2t.net/ark:/99152/p0d.json.

To cite a period from it: (1) find it in the client (client.perio.do), copy the permalink from
the Period > View page and note the "Defined by" authority; (2) write *label, as defined by
authority (permalink)*, never the bare label, because the same label carries several
definitions with different dates.

Never inherit a period without naming whose it is. The same span is cut differently by
dynastic, economic, religious, art-historical and global-history schemes. The tables below
are *conventional* cuts with the dates given by the source cited; they are a start, not a
verdict.

### China 中国
Source: https://en.wikipedia.org/wiki/Template:History_of_China and https://www.clcindex.com/category/K2/ (with K23, K24, K25 subpages) (accessed 2026-08-25)

| Dynasty or period | Dates |
|---|---|
| 夏 Xia (traditional) | c. 2070-1600 BCE |
| 商 Shang | c. 1600-1046 BCE |
| 周 Zhou: 西周 Western 1046-771; 东周 Eastern 771-256 (春秋 Spring and Autumn 770-476; 战国 Warring States 475-221) | c. 1046-256 BCE |
| 秦 Qin | 221-207 BCE |
| 汉 Han: 西汉 Western 202 BCE-9 CE; 新 Xin 9-23; 东汉 Eastern 25-220 | 202 BCE-220 CE |
| 三国 Three Kingdoms | 220-280 |
| 晋 Jin: 西晋 266-316; 东晋 317-420; 十六国 Sixteen Kingdoms 304-439 | 266-420 |
| 南北朝 Northern and Southern dynasties | 420-589 |
| 隋 Sui | 581-618 |
| 唐 Tang (武周 Wu Zhou 690-705) | 618-907 |
| 五代十国 Five Dynasties and Ten Kingdoms | 907-979 |
| 辽 Liao 916-1125; 宋 Song 960-1279 (北宋 960-1127, 南宋 1127-1279); 西夏 Western Xia 1038-1227; 金 Jin 1115-1234 | 916-1279 |
| 元 Yuan | 1271-1368 |
| 明 Ming | 1368-1644 |
| 清 Qing (后金 Later Jin 1616-36; name Qing from 1636) | 1644-1912 |
| 中华民国 Republic of China (mainland) | 1912-1949 |
| 中华人民共和国 People's Republic of China | 1949- |

The conventional cuts as the Chinese Library Classification (中国图书馆分类法, class K2;
clcindex.com is a third-party mirror of the schedule) encodes them: K22 奴隶社会 c. 21st c.-475
BCE; K23 封建社会 475 BCE-1840 with K231 战国, K232 秦汉 221 BCE-220 CE, K235 三国晋南北朝
220-589, K24 隋唐至清前期 581-1840 (K241 隋, K242 唐, K243 五代十国 907-979, K244 北宋, K245
南宋, K246 辽金 916-1234, K247 元, K248 明 1368-1663, K249 清前期 1616-1840); K25
半殖民地半封建社会 1840-1949 with K251 旧民主主义革命时期 1840-1919 and K26 新民主主义革命时期
1919-1949; K27 中华人民共和国时期 1949-. Hence the textbook cuts 先秦 (to 221 BCE) / 秦汉 /
魏晋南北朝 / 隋唐五代 / 宋辽金元 / 明清 (to 1840) / 近代 1840-1919 / 现代 1919-1949 / 当代
1949-. Usage outside the PRC often runs 近代 to 1949 and 现代 from 1949, and Japanese sinology
after Naitō Konan 内藤湖南 opens 近世 with the Song (the "Tang-Song transition") (unverified).

### Japan 日本
Source: https://ja.wikipedia.org/wiki/Template:日本の歴史 and https://en.wikipedia.org/wiki/History_of_Japan (section headings) (accessed 2026-08-25)

| Era 時代 | Dates (Japanese convention) | Notes |
|---|---|---|
| 縄文 Jōmon; 弥生 Yayoi; 古墳 Kofun | c. 14,000 BCE-c. 8th c. BCE; to c. 250-300 CE; c. 250/300-538 | prehistoric and protohistoric |
| 飛鳥 Asuka | 592-710 | en.wikipedia starts it at 538 (arrival of Buddhism) |
| 奈良 Nara | 710-794 | |
| 平安 Heian | 794-1185 | 平氏政権 Taira rule 1167-85 |
| 鎌倉 Kamakura | late 12th c.-1333 | usually 1185 |
| 建武の中興 Kenmu Restoration | 1333-1336 | |
| 室町 Muromachi | 1336-1573 | 南北朝 Nanboku-chō 1337-92; 戦国 Sengoku 1467/1493-1573; en.wikipedia 1333-1568 |
| 安土桃山 Azuchi-Momoyama | 1573-1603 | en.wikipedia 1568-1600 |
| 江戸 Edo | 1603-1868 | 鎖国 sakoku 1639-1854; 幕末 bakumatsu 1853-68; en.wikipedia 1600-1868 |
| 明治 Meiji; 大正 Taishō; 昭和 Shōwa; 平成 Heisei; 令和 Reiwa | 1868-1912; 1912-26; 1926-89; 1989-2019; 2019- | 戦前 / 戦後 cut at 1945 |

Broader Japanese labels: 古代 (Asuka to Heian), 中世 (Kamakura to Muromachi), 近世
(Azuchi-Momoyama and Edo), 近代 (Meiji to 1945), 現代 (1945-) (unverified).

### Korea 한국 / 조선
Source: https://en.wikipedia.org/wiki/Template:History_of_Korea (accessed 2026-08-25)

| Period | Dates |
|---|---|
| 고조선 Gojoseon | (traditional 2333)-108 BCE; Han commanderies after 108 BCE |
| 삼국 Three Kingdoms: 고구려 Goguryeo 37 BCE-668; 백제 Baekje 18 BCE-660; 신라 Silla 57 BCE-935; 가야 Gaya 42-562 | 57 BCE-668 |
| 남북국 North-South States: 통일신라 Unified Silla 668-892; 발해 Balhae 698-926 | 668-926 |
| 후삼국 Later Three Kingdoms: 후백제 Later Baekje 892-936; 태봉 Taebong 901-918; Later Silla 892-935 | 892-936 |
| 고려 Goryeo (Mongol domination 1270-1356) | 918-1392 |
| 조선 Joseon | 1392-1897 |
| 대한제국 Korean Empire | 1897-1910 |
| Japanese rule 日帝強占期 | 1910-1945 |
| Division; 대한민국 ROK and 조선민주주의인민공화국 DPRK | 1945-; both states from 1948 |

### Islamic world
Source: https://en.wikipedia.org/wiki/Caliphate (section headings) and the infoboxes of https://en.wikipedia.org/wiki/Seljuk_Empire, Delhi_Sultanate, Mamluk_Sultanate, Ilkhanate, Timurid_Empire, Ottoman_Empire, Safavid_Iran, Mughal_Empire (accessed 2026-08-25)

| Polity | Dates |
|---|---|
| الخلافة الراشدة Rashidun caliphate | 632-661 |
| الأمويون Umayyad caliphate (Damascus) | 661-750 |
| العباسيون Abbasid caliphate: Baghdad 750-1258; under the Mamluks at Cairo 1261-1517 | 750-1517 |
| Umayyads of Córdoba 929-1031; الفاطميون Fatimids 909-1171; الموحدون Almohads 1147-1269 | rival caliphates |
| Seljuk empire 1037-1194; Delhi sultanate 1206-1526; Mamluk sultanate 1250-1517; Ilkhanate 1256-1335; Timurids 1370-1507 | "middle periods" |
| Ottoman empire c. 1299-1922 (Ottoman caliphate 1517-1924); Safavid Iran 1501-1736; Mughal empire 1526-1857 | the three "gunpowder empires" |

Marshall Hodgson's cuts (formative period to c. 945; middle periods c. 945-1503; gunpowder
empires and modern times) are the usual scholarly alternative to dynastic dating (unverified).

### Europe and the West
Source: https://en.wikipedia.org/wiki/Periodization, https://en.wikipedia.org/wiki/Middle_Ages, https://en.wikipedia.org/wiki/Late_antiquity, https://en.wikipedia.org/wiki/Early_modern_period (accessed 2026-08-25)

| Period | Conventional dates |
|---|---|
| Classical antiquity | c. 8th c. BCE-5th c. CE; 476 (end of the Western empire) is the textbook cut |
| Late antiquity | 3rd-7th c. CE (some run it to the 8th) |
| Early Middle Ages | c. 5th-10th c. |
| High Middle Ages | after 1000-c. 1300 |
| Late Middle Ages | c. 1300-1500 |
| Early modern | c. 1500-1800 (start placed 1500-1600, end 1700-1800, by field) |
| Modern | c. 1800-1945 (the nineteenth century is sometimes split off) |
| Contemporary | 1945-; in French and Romance historiography *époque contemporaine* starts in 1789 and "modern" means early modern |

English-language world history runs Prehistory / Ancient / Late antiquity / Post-classical /
Early modern / Modern / Contemporary.

### The Annales three durations
Source: https://en.wikipedia.org/wiki/Longue_durée (accessed 2026-08-25)

Fernand Braudel ("Histoire et sciences sociales: la longue durée", *Annales* 1958) stacked
three time-scales: *l'événement*, the short-term event (François Simiand's *histoire
événementielle*, the chronicler's and journalist's time); *la conjoncture*, medium-term cycles
of decades or centuries (prices, demography, an industrial revolution); and *la longue durée*,
near-immobile structures (geography, climate, "old attitudes of thought and action, resistant
frameworks dying hard"). Ask which of the three your question lives on, and whether the plan
has a task on each.

## 3. Place facet
Source: https://www.getty.edu/research/tools/vocabularies/tgn/about.html, https://pleiades.stoa.org/, https://chgis.fas.harvard.edu/, https://whgazetteer.org/ (accessed 2026-08-25)

- **Getty TGN** (Thesaurus of Geographic Names), https://www.getty.edu/research/tools/vocabularies/tgn/ :
  global, multilingual, prehistory to present, scoped to art, architecture and archaeology;
  inhabited places, nations, empires, archaeological sites, lost settlements and physical
  features, with place types, dates and coordinates. Moves to a new platform from autumn 2026.
- **Pleiades**, https://pleiades.stoa.org/ : a community-built, peer-reviewed gazetteer and
  graph of *ancient* places, densest for the Mediterranean, the Nile and south-west Asia,
  thinner across Central Asia and India; open licence, machine-readable.
- **CHGIS** (China Historical GIS), https://chgis.fas.harvard.edu/ : a free database of
  placenames and historical administrative units for the Chinese dynasties, a base GIS layer;
  feeds the China Biographical Database and the Temporal Gazetteer.
- **World Historical Gazetteer**, https://whgazetteer.org/ : a platform for linking historical
  place records across time and language (47 million places, 67 million toponyms, phonetic
  search across scripts as of June 2026; API and geocoding).

Scale ladder (editorial): site, then locality (village, town, quarter), region (county,
prefecture, valley, diocese), polity (state, empire), macro-region (East Asia, the
Mediterranean, the Indian Ocean), network (trade routes, pilgrimage circuits, diasporas,
correspondence), world. A question usually lives on one rung; give the plan at least one task
that looks from the rung above and one from the rung below. Always ask what contemporaries
called the place and which gazetteer identifier you will use for it.

## 4. Source-type / genre facet

### Historians' typology: remains vs tradition
Source: https://de.wikipedia.org/wiki/Quelle_(Geschichtswissenschaft) (accessed 2026-08-25)

Johann Gustav Droysen's *Historik* divided sources three ways, *Überreste* (remains), *Quellen*
(narrative sources) and *Denkmäler* (monuments); Ernst Bernheim (*Lehrbuch der historischen
Methode*) reduced this to the pair still taught. **Überreste**, remains: "everything that has
remained directly from the events" (accounts, charters, buildings, bodies, surviving
institutions and languages). **Tradition**: "everything from the events that has passed
through and been rendered by human apprehension" (chronicles, speeches, letters that report,
memoirs). The same document can be either, depending on the question: a letter is tradition
for the event it reports and a remain for the fact that A wrote it to B. Remains weigh more
because they were not made to inform posterity, but they too can be wrong or fraudulent. The
Anglophone primary / secondary / tertiary ladder (evidence from the time; scholarship about
it; reference works digesting scholarship) is orthogonal: it grades distance from the event,
not intention (editorial).

### Getty AAT facets
Source: https://www.getty.edu/research/tools/vocabularies/aat/about.html (accessed 2026-08-25)

The Art & Architecture Thesaurus arranges its concepts under eight facets ordered from
abstract to concrete: **Associated Concepts** (ideas, ideologies, critical concerns);
**Physical Attributes** (attributes and properties, conditions and effects, design elements,
colour); **Styles and Periods**; **Agents** (people, organisations, living organisms);
**Activities** (disciplines, functions, events, physical and mental activities, processes and
techniques); **Materials**; **Objects** (object groupings and systems, object genres,
components, built environment, furnishings and equipment, visual and verbal communication);
**Brand Names**. Use it to name a kind of thing or process. It has Styles and Periods and
Agents but no place facet; that is TGN's job.

### Iconclass
Source: https://iconclass.org/help/outline (accessed 2026-08-25)

Iconclass, the classification of subjects in images, has ten main divisions: 0 Abstract,
Non-representational Art; 1 Religion and Magic; 2 Nature; 3 Human Being, Man in General;
4 Society, Civilization, Culture; 5 Abstract Ideas and Concepts; 6 History; 7 Bible;
8 Literature; 9 Classical Mythology and Ancient History; 450 basic categories sit beneath
them. For any question with visual evidence walk the ten once: images of a subject are filed
where the textual scholar never looks.

### Working list of source kinds (editorial)

| Kind | Chinese tradition | Western tradition |
|---|---|---|
| Official history, chronicle | 正史 (二十四史), 实录, 起居注, 会要, 通鉴 | annals, chronicles, state papers |
| Archives | 档案 (内阁、军机处; 巴县、南部、淡新), 文书 (敦煌、吐鲁番、黑水城、徽州) | state, church, notarial, municipal, corporate archives |
| Gazetteers, geography | 方志 (总志、省志、府志、县志、乡镇志), 地理志 | topographies, surveys, cadastres, inquests |
| Collected works, letters | 文集 (别集、总集), 尺牍, 奏议, 日记 | opera omnia, correspondence, diaries, ego-documents |
| Notebooks, miscellanies (biji) | 笔记, 类书, 丛书, 小说 | commonplace books, encyclopaedias, florilegia |
| Inscriptions, epigraphy | 金石, 碑刻, 甲骨, 简牍, 墓志 | inscriptions, papyri, ostraca, coins |
| Contracts, accounts | 契约, 账簿, 鱼鳞图册, 黄册 | deeds, account books, probate inventories, tax rolls |
| Genealogies | 族谱 / 家谱 / 宗谱 | genealogies, heraldic visitations, parish registers |
| Newspapers, periodicals | 报刊 (申报、大公报), 期刊 | newspapers, periodicals, pamphlets |
| Oral | 口述史, 民间传说, 曲艺 | oral history, folklore, song |
| Images, maps | 图像, 舆图, 版画, 年画, 摄影 | paintings, prints, maps, photographs, film |
| Objects, sites, landscape | 器物, 遗址, 建筑, 田野 | material culture, excavation, standing buildings, landscape |
| Law, case files | 律例, 会典, 判牍, 刑科题本, 讼案 | codes, court rolls, trial records, inquisitions |
| Religious, ritual | 经典, 道藏, 佛藏, 科仪, 宝卷, 善书 | scripture, liturgy, hagiography, sermons, confraternity records |

Tick each row: which kinds exist for your question, which you have read, which you have not,
and which do not survive at all, and why (the archive's own bias, section 6).

## 5. Method facet

### SAGE Research Methods "Methods Map"
Source: https://methods.sagepub.com/methods-map (accessed 2026-08-25; the map is drawn by script and its terms could not be read directly; the top levels below follow the transcription by Lluís Codina, https://www.lluiscodina.com/taxonomy-research-methods-sage/, 2018) (unverified)

The Methods Map is a browsable thesaurus of method concepts: a definition, broader terms to
the left, narrower to the right, related terms below. Its top categories: 01 Key concepts in
research; 02 Philosophy of research; 03 Research ethics; 04 Planning research; 05 Research
design; 06 Data collection; 07 Data quality and data management; 08 Qualitative data
analysis; 09 Quantitative data analysis; 10 Communicating and disseminating research;
11 Researcher development. Under design and analysis the first split is quantitative /
qualitative / mixed methods. Ask which of design, collection and analysis the plan actually
specifies; most plans specify only collection.

### ELSST (CESSDA)
Source: https://elsst.cessda.eu/ and https://thesauri.cessda.eu/elsst/en/ (Version 6, 2025; REST API queried) (accessed 2026-08-25)

The European Language Social Science Thesaurus (3,470 concepts, 15 languages, CC BY-SA;
grown out of the UK Data Service's HASSET (unverified)) has no single methods hierarchy.
Methodological terms sit among its 249 top concepts, mostly without narrower terms:
METHODOLOGY; DATA COLLECTION METHODOLOGY; MODE OF DATA COLLECTION; FREQUENCY OF DATA
COLLECTION; SAMPLING PROCEDURES; SURVEYS; INTERVIEWS (DATA COLLECTION); OBSERVATION (DATA
COLLECTION); MASS OBSERVATION; TIME METHODS (RESEARCH); MEASUREMENTS; EVALUATION; CONTENT
ANALYSIS; DATA ANALYSIS; RESEARCH (narrower: RESEARCH METHODOLOGY, SOCIAL RESEARCH, and
research by field); STATISTICAL ANALYSIS under STATISTICS. Useful as a vocabulary for
social-science data, not as a map of methods.

### Methods used in the humanities (editorial)

- Close reading: what one text does, line by line; blind to what it shares with a thousand others.
- Philology and textual criticism (校勘, 辨伪, 训诂; recension, stemmatics): which text existed
  when and what its words meant then; the precondition for everything else.
- Prosopography: collective biography of a defined population (officials, monks, merchants);
  shows structure where narrative sees individuals.
- Network analysis: who was linked to whom; needs relational sources (letters, genealogies,
  co-signatures, examination lists).
- GIS and spatial history: where, at what distance, over what terrain; needs a gazetteer.
- Quantitative and serial history: counts over time (prices, births, cases, degrees); needs
  series, and a periodisation to break them.
- Oral history: memory and meaning of the living; retrospective by definition.
- Ethnography and fieldwork: practice as observed now, projected backwards with care.
- Discourse analysis and conceptual history (Begriffsgeschichte): how words and categories
  change, and who used them.
- Computational text analysis: patterns across a corpus; inherits the corpus's biases.
- Comparative method: controlled comparison of cases; the choice of unit is the argument.
- Counterfactual reasoning: what had to be true for the outcome; disciplines causal claims.

## 6. Facets no library scheme has (editorial)

| Facet | Ask |
|---|---|
| Perspective, whose view | State or subject; elite or commoner; men or women; majority or minority; insider or outsider. And the archive's own bias: who could write, whose papers were kept, what was never recorded, what was destroyed. |
| Scale | Micro (a person, a village, a case), meso (an institution, a region, a generation), macro (an empire, a civilisation, a century). Which scale is the plan at, and does the claim survive at the others? |
| Language of the sources | Which languages and scripts hold the evidence, which of them the plan can read, and what the translations lose. |
| Medium | Manuscript, print, oral, image, object, born-digital; each with its own pattern of survival, circulation and forgery. |
| Evidence quality | Direct vs hearsay; contemporary vs retrospective; interested vs disinterested; unique vs corroborated. |
| Agency vs structure | Is the question about what people chose or about what constrained them? A plan with only actors misses institutions, environment and demography; one with only structures misses decisions. |

Each of these gets one line in the plan even when the answer is "not relevant here"; the line
is what proves the dimension was considered.
