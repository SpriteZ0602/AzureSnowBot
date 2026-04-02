---
name: python-coder
description: 编写并运行 Python 程序。当用户要求写代码、写脚本、跑程序、调试 Python 时使用。
---

# Python 编程技能

## 工作流程

1. 用 `write_file` 把代码写到 `data/admin/workspace/` 目录下
2. 告诉碧碧你要执行什么命令，等他确认
3. 确认后用 `run_command` 执行：
   ```
   D:\Anaconda\envs\ForBot\python.exe data/admin/workspace/脚本名.py
   ```

## 环境信息

- Python 环境：`D:\Anaconda\envs\ForBot\python.exe`（Conda 环境 ForBot）
- 工作目录：代码文件放在 `data/admin/workspace/`
- 安装包：`D:\Anaconda\envs\ForBot\python.exe -m pip install 包名`

## 规则

- 代码文件统一放 `data/admin/workspace/` 下，不要放其他位置
- 执行前先告诉碧碧要执行什么，等确认
- 如果需要安装新的包，也要先告诉碧碧
- 如果代码报错，读错误信息，修改后重新写入并执行
- 不要用 `input()` 等需要交互输入的代码（命令行不支持）
- 输出结果超长时，加 `[:100]` 之类的截断

## 示例

用户："帮我写个脚本，把 data/admin/workspace/input.csv 的第二列求和"

1. write_file("data/admin/workspace/sum_col.py", 代码内容)
2. 告诉碧碧："我写好了 sum_col.py，准备用 ForBot 环境执行，可以吗？"
3. 碧碧确认后：run_command("D:\\Anaconda\\envs\\ForBot\\python.exe data/admin/workspace/sum_col.py")
4. 返回结果
