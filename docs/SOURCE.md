# 适配层源码

本仓库公开 Codex 适配层和安装脚本。原作引擎、运行环境及动画素材随 Releases 的安装包提供，不重复放入 Git 历史。

- `codex-integration/bridge/`：完整版的原作加载、Codex 联动、专属控制栏、任务卡和聊天适配。
- `packaging/compact_install.py`：完整版安装脚本，对应安装包中的 `data/install.py`。
- `packaging/native_install.ps1`：原生简化版安装脚本，对应安装包中的 `data/install.ps1`。
- `packaging/uninstall_pet.ps1`：两版共用的卸载脚本，停止对应助手并清理匹配的本地注册。
- `packaging/package_commands.py`：生成安装包内的卸载 CMD 入口，让卸载脚本从临时目录运行。
- `tools/alpha-cleanup/`：独立的白龙透明边缘清理工具、依赖说明与测试，不需要 Codex。
- `ASSETS.md`：保留原作的素材说明；本适配项目的发布与署名说明见 `NOTICE.md`。

需要运行或调试完整版时，先下载并解压完整版安装包；其 `data` 目录提供所需引擎、素材和运行时。仓库中的适配文件与包内的 `data/codex-integration/bridge/` 对应，普通使用者直接使用安装包即可。

原作项目及引擎相关说明请查看 [上游项目](https://github.com/jingh12312-rgb/bailong-desktop-pet)。
