# 适配层源码与文档

本仓库公开 v0.5.1.1 的适配代码、测试、打包与安装脚本，以及架构和流程文档。
原作者引擎 v0.5.1、运行环境、音效与动画随完整安装包提供，不纳入 Git 历史。

- `codex-integration/bridge/`：监督、状态观察、引擎加载、行为、窗口、聊天与历史适配。
- `codex-integration/edge-repair/`：透明 WebM 审计和去噪开发工具。
- `packaging/`：安装、卸载、打包与隔离验证。
- `docs/项目架构与流程-v0.5.1.1.md`、`docs/diagrams/`：架构与流程，包含 PNG、SVG、Mermaid。

开发时先完整解压 Release 安装包，将本仓库对应文件复制到其 `data` 目录，
以便使用内置引擎、Python、解码器与动画；普通使用者直接使用完整安装包即可。
动画工具的额外开发依赖为 OpenCV、NumPy、Pillow。

本版 Release 手动附件仅提供完整安装包与 SHA-256 校验文件。
旧原生简化版及早期去噪工具代码保留历史用途，本版不新增对应下载附件。
