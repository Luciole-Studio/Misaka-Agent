# MISAKA SIS实际分工核验

核验窗口：2026-09-20 15:27–15:31 JST。运行：`r_b55f2c1cf5`。

只读读取Board、消息、会话目录及已落盘产物。未发消息、未运行MISAKA工具、未重启或修改运行状态。审计脚本/报告只写本临时目录。

## 结论

已启动9张卡的初始具体问题逐字匹配LO计划中的相应question，9个独立根会话、9份不同首条任务。扫描其20个子代理，共29份会话中的1374次工具调用；抽取并核对检索主题、委派边界、写入路径及交付章节，已观察执行与各自主责一致。未发现跨卡认领、跨卡TODO更新或向其他卡交付目录写入的记录。

不是一名SIS身份只能做一张卡：10032在6个独立卡会话中做6项不同研究。任务存在明确的主题交集、上下游依赖和材料复用，不主张零重复检索。

## 结构检查

18张计划卡的assignee与Board一致；9个已启动卡的catalog身份与assignee一致；9个首条任务的research question与原计划逐字一致。54次misaka_todo调用未发现跨卡mark；72次write/edit调用的目标均含本卡ID；136条artifact_written事件未发现其他卡目录。91条相关消息中，指定to_task的收件SIS均匹配卡片归属。

## 按实际会话核验

### C1 · SIS 10032

1803–1812战略分岔地图、同时代选项与反事实方法；不重复C11–C13深潜

- 卡片：`t_3dc3c2`；独立原生session：`01a0bd35-a3c6-7286-929d-edf90efbe3e9`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_3dc3c2/2026-09-20T05-06-44-294Z_01a0bd35-a3c6-7286-929d-edf90efbe3e9.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第18行：`Napoleon Berlin decree 1806 Tilsit treaty 1807 text articles mediation England`；返回记录第23行。
  - 会话第18行：`Tetlock Belkin counterfactual five criteria clarity consistency cotenability historical consistency theoretical consistency`；返回记录第24行。
  - 会话第25行：`Napoleon Frederick William letter October 12 1806 peace war Erfurt Prussia`；返回记录第30行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_3dc3c2/01a0bd35-a3c6-7286-929d-edf90efbe3e9/subagents/agent-a1bbf0717d7bcbf90.jsonl)：C1地图研究1809战后处置、1810联姻/俄波保证公约、1811–1812避免战争或有限战争选项。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_3dc3c2/01a0bd35-a3c6-7286-929d-edf90efbe3e9/subagents/agent-a622181b6b49d12f8.jsonl)：研究C1 1803–05地图节点：布洛涅/1805舰队机动、爱尔兰、Fulton潜艇鱼雷。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_3dc3c2/01a0bd35-a3c6-7286-929d-edf90efbe3e9/subagents/agent-a93e19d37b167bdcb.jsonl)：研究C1地图1806 Fox–Yarmouth–Lauderdale和谈，以及1808 Erfurt对英联合和平提议。
- 实际产物：[c1_pod_dossier.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_3dc3c2/c1_pod_dossier.md)。

### C2 · SIS 10032

英法舰队实存/在建、人力、财政贸易数据口径；CSV切片与缺口

- 卡片：`t_104134`；独立原生session：`01a0bd35-a3c6-76be-b1da-88f7948aeb59`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_104134/2026-09-20T05-06-44-294Z_01a0bd35-a3c6-76be-b1da-88f7948aeb59.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第20行：`James naval history 1803 abstract British navy 189 181`；返回记录第22行。
  - 会话第29行：`Royal Navy ships in commission 1803 1804 1805 1810 1815 Clowes table`；返回记录第32行。
  - 会话第47行：`site.e-rara.ch "James" "1803" "ABSTRACT"`；返回记录第48行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_104134/01a0bd35-a3c6-76be-b1da-88f7948aeb59/subagents/agent-a349a6ea33174ed3b.jsonl)：为C2收集1793–1815皇家海军与法国海军人力、战俘存量、波罗的海木材桅杆大麻供应数量。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_104134/01a0bd35-a3c6-76be-b1da-88f7948aeb59/subagents/agent-a498db257a1693bdc.jsonl)：为C2英法海军战争经济数据找并实际读英国1793–1815年度税收、国债、海军财政或贸易1808–12序列。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_104134/01a0bd35-a3c6-76be-b1da-88f7948aeb59/subagents/agent-ae96eec824a680040.jsonl)：为C2查法国1807–14海军重建实绩，核心Richard Glover The French Fleet 1807–1814 JMH 1967，需afloat/building年度分解，安特卫普/威尼斯/热那亚船厂实绩。
- 实际产物：[c2_naval_econ_data.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_104134/c2_naval_econ_data.md)。

### C5 · SIS 10032

西班牙政体选项、拉美juntas机制、白银通道与海地

- 卡片：`t_c2dbca`；独立原生session：`01a0bd35-a3c6-7acb-8711-0da347963e49`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_c2dbca/2026-09-20T05-06-44-294Z_01a0bd35-a3c6-7acb-8711-0da347963e49.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第11行：`Napoleon Ferdinand letter 16 April 1808 marriage Murat 29 March 1808 authenticity`；返回记录第16行。
  - 会话第17行：`Napoleon Ferdinand 16 April 1808 letter marriage niece Escoiquiz`；返回记录第20行。
  - 会话第17行：`Estatuto Bayona 1808 texto constitucion 87 88 89`；返回记录第21行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_c2dbca/01a0bd35-a3c6-7acb-8711-0da347963e49/subagents/agent-a12e7caf7703ad509.jsonl)：研究C5限定部分：Rodríguez O./Guerra正统性危机与帝国失能/Adelman解释的真实差异，juntas在国王未废下是否仍启动；Robertson France and Latin-American Independence中法国代理介入具体记录；海地1802复奴法律、1815究竟废奴还是仅禁止奴隶贸易。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_c2dbca/01a0bd35-a3c6-7acb-8711-0da347963e49/subagents/agent-ab6b784abde2f3288.jsonl)：研究C5限定部分：英国1808原拟西属美洲而改派葡萄牙的远征，要求Fortescue/Muir可读原文精确位置；1806–07拉普拉塔和巴西宫廷用于反事实类比；Marichal/Buist有关墨西哥白银Ouvrard Hope交易机制。
- 实际产物：[c5_iberia_americas.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_c2dbca/c5_iberia_americas.md)。

### C6 · SIS 10032

意大利分冠与继承、罗马王、莱茵邦联、普奥安排

- 卡片：`t_7da32d`；独立原生session：`01a0bd35-a3dd-7195-97a7-d1407d188d6e`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_7da32d/2026-09-20T05-06-44-317Z_01a0bd35-a3dd-7195-97a7-d1407d188d6e.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第18行：`senatus consulte 17 février 1810 Rome seconde ville roi de Rome article 10`；返回记录第21行。
  - 会话第18行：`Napoleon Italie une seule nation Sainte Hélène Mémorial Italie Eugène unification`；返回记录第22行。
  - 会话第23行：`"Sénatus-consulte" "17 février 1810" "Article" Rome`；返回记录第24行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_7da32d/01a0bd35-a3dd-7195-97a7-d1407d188d6e/subagents/agent-a4727f507fad3c2b4.jsonl)：研究C6普鲁士与奥地利1807–12。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_7da32d/01a0bd35-a3dd-7195-97a7-d1407d188d6e/subagents/agent-a80fd67c0a11bbfda.jsonl)：研究C6莱茵邦联整合与民族主义。
- 实际产物：[c6_italy_germany.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_7da32d/c6_italy_germany.md)。

### C7 · SIS 10032

Decaen训令、芬肯施泰因条约、波斯通道与印度远征约束

- 卡片：`t_c17367`；独立原生session：`01a0bd57-079f-79df-98e6-48f169baf4e7`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_c17367/2026-09-20T05-43-12-543Z_01a0bd57-079f-79df-98e6-48f169baf4e7.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第20行：`Bayly imperial meridian Napoleon India threat 1808 Malcolm Minto`；返回记录第22行。
  - 会话第38行：`Caulaincourt "Constantinople" "empire du monde"`；返回记录第39行。
  - 会话第47行：`Paul 1801 India expedition plan authenticity 35000 Orlov Napoleon`；返回记录第48行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_c17367/01a0bd57-079f-79df-98e6-48f169baf4e7/subagents/agent-a1922ee6a05036bb6.jsonl)：为C7独立检索并读原文：1807芬肯施泰因条约尤其印度通道条款；Encyclopaedia Iranica Gardane/France Persia 伊朗侧证据。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_c17367/01a0bd57-079f-79df-98e6-48f169baf4e7/subagents/agent-ac7704790929ff714.jsonl)：检索并实际读取1802–03 Napoleon Decaen instructions原文（观察待机、4年、英属印度），可用Prentout/Picard/Correspondance。
- 实际产物：[c7_orient_india.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_c17367/c7_orient_india.md)。

### C8 · SIS 10037

政治治理、教会、正统性、摄政继承与压力测试

- 卡片：`t_b09c54`；独立原生session：`01a0bd59-64c2-72c7-a8a9-616b13d97327`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_b09c54/2026-09-20T05-45-47-458Z_01a0bd59-64c2-72c7-a8a9-616b13d97327.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第14行：`site.napoleon.org Malet 1812 Napoleon II regency 1813`；返回记录第16行。
  - 会话第14行：`constitution an XII 1804 titre régence article 18 19`；返回记录第17行。
  - 会话第20行：`"1813" "régence" "sénatus" Marie Louise 5 février`；返回记录第24行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_b09c54/01a0bd59-64c2-72c7-a8a9-616b13d97327/subagents/agent-a15d696203826f326.jsonl)：为C8政治秩序报告研究宗教1809绝罚1811会议、荷兰路易1810退位、蒂罗尔1809及威斯特伐利亚财政过载。
- 实际产物：[c8_cases_sources.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_b09c54/c8_cases_sources.md)。
- 实际产物：[c8_order_model.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_b09c54/c8_order_model.md)。

### C9 · SIS 10038

英国税债信用与法国贡赋/封赏、财政再生产及和平转型

- 卡片：`t_bef693`；独立原生session：`01a0bd5a-d0db-7d80-bef1-8ad479a3afcb`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_bef693/2026-09-20T05-47-20-667Z_01a0bd5a-d0db-7d80-bef1-8ad479a3afcb.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第17行：`Napoleon finances domaine extraordinaire 1810 1813 200 millions dotations Branda`；返回记录第19行。
  - 会话第25行：`"3 mars 1810" "dotations" "vingt"`；返回记录第28行。
  - 会话第34行：`site.banque-france.fr 1806 gouverneur Napoléon statuts 1808 banque`；返回记录第35行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_bef693/01a0bd5a-d0db-7d80-bef1-8ad479a3afcb/subagents/agent-a22e87fdba5babe4d.jsonl)：为C9核验意大利对法转移/预算比例、威斯特伐利亚债务、普鲁士赔款及收入倍数、西班牙占领净成本。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_bef693/01a0bd5a-d0db-7d80-bef1-8ad479a3afcb/subagents/agent-a5934b27881e29a3a.jsonl)：为C9查英国1793–1815税债财政，1797停兑、1810–11危机和1815后偿债负担。
- 实际产物：[c9_war_finance.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_bef693/c9_war_finance.md)。

### C10 · SIS 10036

ACJR/Juhász/Buggle/Lecce–Ogliari等因果证据与外推边界

- 卡片：`t_f3c44f`；独立原生session：`01a0bd7b-521d-7a60-90b8-dda66989653f`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_f3c44f/2026-09-20T06-22-50-909Z_01a0bd7b-521d-7a60-90b8-dda66989653f.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第13行：`Acemoglu Cantoni Johnson Robinson 2011 consequences radical reform French Revolution pdf`；返回记录第18行。
  - 会话第13行：`Kopsidis Bromley 2016 French revolution German industrialization new institutional economics pdf`；返回记录第19行。
  - 会话第13行：`Juhasz 2018 Temporary Protection Technology Adoption Evidence Napoleonic Blockade pdf`；返回记录第20行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_f3c44f/01a0bd7b-521d-7a60-90b8-dda66989653f/subagents/agent-a14a497d552def26e.jsonl)：C10独立文献审计：Dincecco财政制度，Ploeckl关税同盟与Keller–Shiue，Franck & Michalopoulos工业化/人力资本（题名年份需实际核实）。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_f3c44f/01a0bd7b-521d-7a60-90b8-dda66989653f/subagents/agent-a80340b7b02f042e9.jsonl)：C10因果证据报告的独立材料任务：取得并阅读Buggle 2016 Law and social capital、Lecce & Ogliari 2019 Institutional Transplants and Cultural Proximity等实际题名以核实为准。
- 实际产物：[culture_audit.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_f3c44f/culture_audit.md)。
- 实际产物：[mechanism_audit.md](/Users/makiko/Documents/exam/nodes/r_b55f2c1cf5/cards/t_f3c44f/mechanism_audit.md)。

### C11 · SIS 10032

布洛涅运力与舰队计划、英国防务、爱尔兰与Fulton

- 卡片：`t_2c338c`；独立原生session：`01a0bd7e-57c9-7e16-9011-37a40efc53e2`。
- 原始会话：[JSONL](/Users/makiko/.misaka/sessions/cards/t_2c338c/2026-09-20T06-26-08-969Z_01a0bd7e-57c9-7e16-9011-37a40efc53e2.jsonl)；首条任务在第5行。
- 实际检索/阅读证据：
  - 会话第23行：`"Desbrière" "projets" "gallica"`；返回记录第24行。
  - 会话第23行：`"Desbrière" "juillet" "1804" "tempête"`；返回记录第25行。
  - 会话第28行：`"projetsettentati"`；返回记录第31行。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_2c338c/01a0bd7e-57c9-7e16-9011-37a40efc53e2/subagents/agent-a4b24b7567b2617e2.jsonl)：C11入侵英国1803-05。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_2c338c/01a0bd7e-57c9-7e16-9011-37a40efc53e2/subagents/agent-a63a09b05ed93bccb.jsonl)：C11入侵英国1803-05研究。
- 子任务：[会话](/Users/makiko/.misaka/sessions/cards/t_2c338c/01a0bd7e-57c9-7e16-9011-37a40efc53e2/subagents/agent-ab642d9c008d3b3ee.jsonl)：C11入侵英国1803-05研究。

## 交叉引用不是改派

- C1明确把自己限定为全景地图，C11单独深潜入侵英国；C11直接读取C1的early_evidence.md后查Desbrière/Corbett与英方防务。
- C1读取C5的spain_evidence.md核对巴约讷史料警告；写入仍在C1目录。
- C9读取C5的britain_silver.md，把白银通道作为财政模型输入；修订仍在C9的c9_war_finance.md。
- C6/C8/C9都会触及威斯特伐利亚，但各自主责分别为地区政治结构、治理合法性、财政汲取。C8与LO消息明确把债务口径交C9，未将未核净成本当事实。
- 2条消息（55、66）发给10037身份而未绑定具体卡，内容为C9向C4/C8共享财政基线与材料，不是转移卡片责任。

## 证据边界

- 其余9张尚未启动卡只能核对计划分工，不宣称已验证其未来行为。
- 本次核验执行主题与归属，不是历史事实/引文准确性或交付完整性的审稿；例如C2报告明确自称部分核验而非全量冻结。
- 日志是截至读取时的记录，正在执行的会话随后可能追加。自动路径/ID检查不等于证明任何可能的间接写入永远不存在。
- 数据：[Board快照](/private/tmp/misaka-focus-audit-20260920/snapshot.json)、[结构检查](/private/tmp/misaka-focus-audit-20260920/checks.json)、[会话证据](/private/tmp/misaka-focus-audit-20260920/sessions.json)、[消息记录](/private/tmp/misaka-focus-audit-20260920/mail.json)。
