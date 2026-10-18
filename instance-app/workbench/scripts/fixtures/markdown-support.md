# Markdown 阅读测试

这份文档用于验证 **加粗**、*斜体*、~~删除线~~、`inline_code` 和 [安全链接](https://example.com)。

**自由意志**（free will）在这里指一种特殊的控制能力：读者需要结合[上下文](https://example.com/context)和`PAP`理解这句话的责任含义。

## 表格与单元格引用

| 项目 | 内容 | 数量 |
| :--- | :---: | ---: |
| 中文 | 表格引用目标 | 128 |
| 英文 | **Bold** and `code` | 256 |
| 分隔符 | a\|b | 512 |
| 长单元格 | This_is_a_deliberately_long_value_to_test_horizontal_overflow_in_a_small_mobile_reader | 1024 |

在表格第二列选中“表格引用目标”，提取节点，再重新打开文档检查高亮。

## 列表

3. 第三项
4. 第四项
   - 子列表一
   - 子列表二
     1. 深层条目

- [ ] 待完成任务
- [x] 已完成任务

## 引用与代码

> 第一段引用，含 **强调**。
>
> 第二段引用。

~~~python
def greet(name):
    return f"Hello, {name}"
~~~

#### 四级标题

##### 五级标题

###### 六级标题

Setext 标题
-----------

行内公式 $x^2 + y^2$，块公式如下：

$$
E = mc^2
$$

## 安全边界

下面的 HTML 应当只显示为文字，不应执行。

<script>window.markdownInjected = true</script>

![图片替代文字](https://example.invalid/should-not-load.png)

[危险链接](javascript:alert(1))
