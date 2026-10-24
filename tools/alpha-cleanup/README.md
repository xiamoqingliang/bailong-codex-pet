# 白龙动画透明边缘清理工具

这是白龙 Codex 适配过程中使用的像素级清理工具。保留当时的清理算法和无损 WebP 写出模块，补充了可指定的输入、输出路径，方便在其他电脑上使用。

适用对象是白龙桌宠 **v0.4.1 的 720×720 透明 WebP 动画**及对应的 `素材清单.json`。本包不附带角色素材，不需要 Codex，也不会读取账号或聊天记录。

## 处理方式

- 结合颜色、透明度、连通区域及与主体的距离，清理符合条件的灰色／灰橄榄色边缘碎点。
- 仅将选中的像素透明度置为 0，不修改 RGB 内容，不重绘角色、不缩放画面。
- 保留每一帧、每帧时长、循环次数和动画背景参数，包括连续相同的帧。
- 对灯泡、宝箱金币、生气符号及部分淡出粒子保留专门的保护区域。
- 输出到独立目录，完成后重新解码核对帧数、时长和 RGB，并检查原文件哈希未改变。

清理规则包含白龙专用的动作 ID 和像素坐标，**不是通用背景移除或任意角色去噪工具**。换角色、修改构图或分辨率时，应先调整规则并预览确认。为了保留特效，部分无法可靠区分的碎点会保留。

## 准备环境

推荐使用 **64 位 Python 3.13**，本包在 Python 3.13.7 下验证。首次安装依赖需要联网，处理素材时不需要联网。

在工具目录打开终端，执行：

```powershell
python -m pip install -r requirements.txt
```

`requirements.txt` 固定了本次验证使用的 Pillow、NumPy 和 OpenCV 版本。建议在独立虚拟环境中安装。

## 使用

`--source` 指向包含 `素材清单.json` 的原始素材目录，**不是桌宠程序根目录**。下面的相对路径请按实际解压位置修改。

先生成对比预览：

```powershell
python repair_assets.py --source "..\BailongPet\outputs\白龙桌宠素材" --output ".\clean-assets" --preview
```

确认预览后，处理整套素材：

```powershell
python repair_assets.py --source "..\BailongPet\outputs\白龙桌宠素材" --output ".\clean-assets" --build --jobs 2
```

也可以先处理一个动作，例如第二套待机：

```powershell
python repair_assets.py --source "..\BailongPet\outputs\白龙桌宠素材" --output ".\idle2-trial" --preview --build --ids idle2 --jobs 1
```

- `--source`：原始素材目录，必填。
- `--output`：单独的输出目录，可自行指定。
- `--report-dir`：可选的报告与预览存放目录。
- `--preview`：输出左右对比图，不改动原素材。
- `--build`：生成处理后的 WebP。
- `--ids`：只处理指定动作，可传多个 ID，例如 `idle idle2 work02`。
- `--jobs`：并行进程数。内存或 CPU 紧张时使用 `1`。
- `--overwrite`：允许替换输出目录中已有的处理结果；默认不覆盖。

只选部分动作时，结果属于单项试验，不代表整套素材已经完成。整套处理请使用不带 `--ids` 的 `--build`，生成完整素材清单和校验结果后，再用于桌宠。工具不会自动替换桌宠正在使用的文件。

无损重编码后的文件可能比原文件更大。请先在独立输出目录检查人物细节、粒子和淡出效果。

## 自检与代码

```powershell
python -m unittest discover -s tests -v
```

基础自检使用临时合成素材，不需要角色图片。若要执行针对白龙原始素材的回归检查，可先设置：

```powershell
$env:BAILONG_SOURCE_DIR = "..\BailongPet\outputs\白龙桌宠素材"
python -m unittest discover -s tests -v
```

- `repair_assets.py`：清理规则、预览、批量处理及校验。
- `webp_exact.py`：逐帧无损 WebP 写出，避免编码器合并连续相同帧。
- `tests/`：写出器、命令行与特效保护回归检查。

原素材来自 [jingh12312-rgb/bailong-desktop-pet](https://github.com/jingh12312-rgb/bailong-desktop-pet)，素材许可仍遵循原作者授权。本包仅提供处理代码与使用说明。
