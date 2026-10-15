import sys

# 故意失败：模拟「文档里指向了不存在的文件」
print("✗ 有 2 处文档链接失效")
print("   - docs/guide.md 引用了 missing-page.md")
print("   - README.md 引用了 docs/old-api.md")
sys.exit(1)
