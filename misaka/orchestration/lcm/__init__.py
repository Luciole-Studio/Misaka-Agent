"""LCM（无损上下文管理）——hermes-lcm 移植（设计 docs/design/lcm.md）。

一期＝数据层：消息库（FTS5）＋摘要 DAG＋CJK 感知 token 估算＋查询构建。
库落 ~/.misaka/lcm.db；压缩链二期、引擎接缝三期、回收工具四期。
源：github.com/stephenschoettler/hermes-lcm（MIT），移植记账见 third_party/PINS.md。
"""
