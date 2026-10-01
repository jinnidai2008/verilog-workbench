# 怎么自己跑起来

> 这份文档写给两类人：**只想看结果的**，和**想跑自己 Verilog 文件的**。

---

## 一、只想看结果（最简单）

双击 `index.html` 就行了。

- 不用装任何东西、不用联网
- 背景视频来自同目录的 `bgvideo.mp4`，所以**别把文件拆开**
- 这个模式是**故意只读**的：
  - 「选择工程」是灰的
  - 「重新运行」是灰的
  - 拖文件进去没反应

**这是预期行为** —— 这份是静态快照，背后没有引擎，只有已经跑好的结果。

---

## 二、想跑自己的 Verilog 文件

需要三样东西，按顺序来。

### 1. Python 3.10+

https://www.python.org/downloads/ —— 安装时**勾上 "Add python.exe to PATH"**。

### 2. EDA 工具链：iverilog + verilator + yosys

**二选一**：

**(A) Windows 便携包 —— 最省事，不用 WSL**

去 https://github.com/YosysHQ/oss-cad-suite-build/releases 下载
`oss-cad-suite-windows-x64`（约 570 MB），解压到任意目录，例如 `D:\oss-cad-suite`。

**不用安装、不用管理员权限。**

**(B) 用 WSL（已有 WSL 才选这个）**

```bash
wsl -d Ubuntu-24.04 -- sudo apt update
wsl -d Ubuntu-24.04 -- sudo apt install -y iverilog verilator yosys
```

### 3. API key（只在"生成新代码"时需要）

去 https://platform.deepseek.com/api_keys 建一个，粘到页面**右下角「API」**按钮里。
**只存在你自己的浏览器里。**

**只探测已有文件的话，不需要 key。**

---

## 三、启动服务

在项目目录开个终端：

```bash
python server.py 8787 --no-browser
```

打开它打印的地址：

```
http://127.0.0.1:8787/workbench.html
```

**这时候按钮就能用了** —— 可以选工程、拖文件、真跑一遍探测。

如果提示找不到 EDA 工具，告诉它工具在哪：

```bash
# Windows 便携包
python refresh.py --backend windows --toolchain-dir D:\oss-cad-suite\bin

# 或者走 WSL
python refresh.py --backend wsl
```

---

## 四、命令行（不用浏览器）

```bash
python refresh.py <工程目录>              # 探测已有工程
python quench.py new "8位计数器"          # 从一句话生成 Verilog
python quench.py bench --suite suites\basic.txt   # 批量跑并出报告
```

---

## 五、会不会花别人的钱？

**只花"运行它的人"的钱**，而且只有「生成」这一步会花（要调 DeepSeek）。
探测已有 Verilog 文件是**免费且离线**的。

**作者的 API key 不在这个包里，也永远不会在。**
