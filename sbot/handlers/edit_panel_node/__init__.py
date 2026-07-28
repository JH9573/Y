"""添加 / 编辑面板 v2node 节点的对话流。

入口:
- 节点列表 [➕ 添加节点]                    -> 新增
- 节点详情 [✏️ 编辑](支持的协议)           -> 编辑

原本是一个 1890 行的单文件,现按职责拆开:
- options.py      常量与选项表(叶子模块)
- state.py        流程编排、通用 helper、_prompt_* 登记表
- steps_basic.py  各协议共用的步骤
- steps_proto.py  协议特有的步骤(cipher / flow / 带宽)
- steps_tls.py    TLS 与 tls_settings 引导
- confirm.py      payload 组装、摘要、提交
- conversation.py 入口与 ConversationHandler 装配

提交成功后会调一次 get_v2nodes 同步整张缓存表,保证本地与面板一致。
"""
from .conversation import register


__all__ = ["register"]
