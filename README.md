# 理论题作答助手

一个 Windows 桌面 GUI 小工具：

- 统一输入一道理论题或带选项的题目
- 自动把题目发送到豆包、DeepSeek、千问、智谱清言、Kimi、ChatGPT 网页端
- 抓取每个平台返回的 `答案：A` / `答案：A,C` 形式结果
- 统计重合度最高的推荐答案

## 当前实现方式

为了兼顾稳定性和登录态保留，程序采用了：

- `Tkinter` 做桌面 GUI
- `Playwright` 控制真实浏览器网页端
- `profiles/` 目录保存每个平台的登录状态

这意味着：

1. 第一次使用时，需要先点 `打开/初始化网页`
2. 浏览器弹出后，分别在每个平台里手动完成登录
3. 登录完成后回到主界面，把题目加入 `任务队列`
4. 平台前面的勾选框表示本次要不要使用该平台；勾选才会初始化和提问，不勾选会跳过
5. 如果要一次导入多道题，可以在输入框里用单独一行 `---` 分隔

## 安装

建议在本目录下创建虚拟环境：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

如果你想优先驱动本机 Edge：

- GUI 里的浏览器下拉框选择 `msedge`
- 确保本机已安装 Microsoft Edge

如果你想优先驱动本机 Chrome：

- GUI 里的浏览器下拉框选择 `chrome`
- 确保本机已安装 Google Chrome

## 启动

```powershell
python app.py
```

## 使用建议

- 输入题目时，尽量把题干和 `A/B/C/D` 选项一起贴进去
- 点 `加入队列` 后，题目会自动排队执行，已完成/已失败/已终止都会在队列表里标出来
- 点 `强制结束` 会终止当前运行中的题目，并把还没开始的题目从队列里移除
- 程序会强制要求各家模型按下面格式回答，便于提取：

```text
答案：B
理由：……
```

- 如果某个平台没有识别到输入框，通常是因为：
  - 还没登录
  - 当前页面不在聊天界面
  - 网页结构改版，需要调整 `providers.py` 或 `automation.py`

## 已知限制

- 这些站点的网页 DOM 改动很频繁，少数平台可能需要针对性修 selector
- 某些站点可能会检测自动化，偶发需要手动刷新页面后再试
- 当前版本更偏“可用原型”，主打先把整条链路跑通

## 关键文件

- `app.py`：桌面 GUI
- `automation.py`：浏览器自动化与答案抓取
- `providers.py`：平台地址与 selector 配置
- `consensus.py`：选项提取与重合度统计
