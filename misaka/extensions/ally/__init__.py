"""协力者（ally）：跑在格子里的第三方 agent CLI（codex / claude / gemini / …）。

与 Sisters 的分工：
- Sister＝misaka 引擎里的御坂，会说 SendMessage，协议级双向通信。
- Peer＝别人家的 CLI，不认识 misaka 任何协议。我们只做一件它不会的事：
  **异步起进程 → 收 stdout → 把回话投进 messages.db**，于是 LO 从**同一个信箱**
  收到协力者的回话，心智模型与使唤御坂一致。

零厂商知识（用户裁定）：不维护"各家 CLI 怎么调"的表——命令行由 LO 每次给全，
续聊的 session id 也由 LO 自己从回话里读、下次自己写进命令行。misaka 只负责执行。
"""
