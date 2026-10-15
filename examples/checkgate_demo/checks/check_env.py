import sys

# 故意失败，但注册表把它标成 warn ⇒ 只警告、不阻塞
print("✗ 缺少可选配置项 EXAMPLE_TOKEN")
sys.exit(1)
