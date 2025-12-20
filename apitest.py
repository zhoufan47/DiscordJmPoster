import requests
import json
import os

# ================= 配置 =================
# 确保端口与 config.json 中的 api_port 一致
API_URL = "http://127.0.0.1:8000/api/publish"
TEST_DIR = "test_files"


# ================= 准备测试文件 =================
def create_test_files():
    if not os.path.exists(TEST_DIR):
        os.makedirs(TEST_DIR)

    # 1. 创建一个假的封面图片 (只要文件存在即可，不需要真实图片内容，但在真实场景应为图片)
    cover_path = os.path.join(TEST_DIR, "test_cover.png")
    with open(cover_path, "wb") as f:
        f.write(b"FAKE IMAGE CONTENT")  # 写入一些假数据

    # 2. 创建一个测试附件文档
    doc_path = os.path.join(TEST_DIR, "test_log.txt")
    with open(doc_path, "w", encoding="utf-8") as f:
        f.write("这是一份测试用的附件内容。\nAPI 测试正常。")

    return cover_path, doc_path


# ================= 发送 POST 请求 =================
def send_post_request(cover_path, doc_path):
    # 获取绝对路径，以防 main.py 无法找到相对路径
    abs_cover_path = os.path.abspath(cover_path)
    abs_doc_path = os.path.abspath(doc_path)

    # 构造 JSON Body
    payload = {
        "comic_id" : "12345678",
        "title": "API 自动化测试帖子",  #帖子标题，即漫画标题
        "content": "这是一条通过 Python 脚本自动发送的测试帖子。\n包含封面和附件。", #实际因为漫画简介内容，包含画师信息等
        "cover": abs_cover_path, # 封面图片的绝对路径
        "tags": ["测试", "自动"],  # 获取的漫画tag
        "attachment": [     # 漫画pdf路径
            abs_doc_path
        ]
    }

    print(f"[-] 正在发送 POST 请求到: {API_URL}")
    print(f"[-] Payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")

    try:
        # 这里使用 json=payload，requests 库会自动设置 Content-Type: application/json
        response = requests.post(API_URL, json=payload)

        print(f"\n[+] 状态码: {response.status_code}")

        if response.status_code == 200:
            print("[+] 发布成功！返回数据:")
            print(json.dumps(response.json(), ensure_ascii=False, indent=4))
        else:
            print("[!] 发布失败。错误信息:")
            print(response.text)

    except requests.exceptions.ConnectionError:
        print("[X] 连接失败：请确保 main.py 正在运行。")
    except Exception as e:
        print(f"[X] 发生错误: {e}")


if __name__ == "__main__":
    print("=== 开始 API 测试 ===")

    # 1. 创建文件
    c_path, d_path = create_test_files()
    print(f"[-] 测试文件已创建: \n    1. {c_path}\n    2. {d_path}")

    # 2. 发送请求
    send_post_request(c_path, d_path)

    print("=== 测试结束 ===")