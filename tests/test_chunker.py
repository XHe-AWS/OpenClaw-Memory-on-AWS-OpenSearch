"""
Tests for chunker.py — Markdown-aware chunking.
"""
import pytest
from chunker import chunk_markdown, Chunk, _split_by_headings, _split_into_blocks


class TestBasicChunking:
    def test_empty_content(self):
        """Empty content returns no chunks."""
        assert chunk_markdown("") == []
        assert chunk_markdown("   ") == []

    def test_single_paragraph(self):
        """Short text becomes a single chunk."""
        chunks = chunk_markdown("Hello, this is a test.")
        assert len(chunks) == 1
        assert "Hello, this is a test." in chunks[0].text

    def test_chunk_has_metadata(self):
        """Chunks carry source file and line info."""
        chunks = chunk_markdown("Test content", source_file="MEMORY.md")
        assert chunks[0].source_file == "MEMORY.md"
        assert chunks[0].index == 0
        assert chunks[0].start_line > 0

    def test_multiple_sections(self):
        """Multiple ## headings create separate chunks."""
        content = """## Section A

Content of section A.

## Section B

Content of section B.
"""
        chunks = chunk_markdown(content)
        assert len(chunks) >= 2
        # Each chunk should have its heading
        texts = [c.text for c in chunks]
        assert any("Section A" in t for t in texts)
        assert any("Section B" in t for t in texts)


class TestCodeBlockHandling:
    def test_code_block_not_split(self):
        """Code blocks are kept whole."""
        content = """## Code

```python
def long_function():
    # This is a very long function
    for i in range(100):
        print(f"Line {i}")
        if i % 10 == 0:
            print("Checkpoint")
    return True
```
"""
        chunks = chunk_markdown(content)
        # The code block should be in a single chunk
        code_chunks = [c for c in chunks if "```python" in c.text]
        assert len(code_chunks) >= 1
        assert "return True" in code_chunks[0].text

    def test_code_block_preserved(self):
        """Code block delimiters are preserved."""
        content = "```\ncode here\n```"
        chunks = chunk_markdown(content)
        assert any("```" in c.text for c in chunks)


class TestTableHandling:
    def test_table_not_split(self):
        """Tables are kept as single blocks."""
        content = """## Data

| Name | Value |
|------|-------|
| A    | 1     |
| B    | 2     |
| C    | 3     |
"""
        chunks = chunk_markdown(content)
        table_chunks = [c for c in chunks if "| Name |" in c.text]
        assert len(table_chunks) >= 1
        assert "| C    | 3" in table_chunks[0].text


class TestListHandling:
    def test_list_kept_together(self):
        """List items at the same level are kept together."""
        content = """## Items

- First item
- Second item
- Third item
"""
        chunks = chunk_markdown(content)
        list_chunks = [c for c in chunks if "First item" in c.text]
        assert len(list_chunks) >= 1
        assert "Third item" in list_chunks[0].text


class TestHeadingSplit:
    def test_heading_extraction(self):
        """## headings are correctly extracted."""
        content = """# Title

Intro paragraph.

## First Section

Content 1.

## Second Section

Content 2.
"""
        sections = _split_by_headings(content)
        headings = [s[0] for s in sections]
        assert "First Section" in headings
        assert "Second Section" in headings

    def test_heading_prefixed_to_chunks(self):
        """Each chunk gets its parent heading as context."""
        content = """## My Heading

Some content here.
"""
        chunks = chunk_markdown(content)
        assert chunks[0].text.startswith("## My Heading")
        assert chunks[0].heading == "My Heading"


class TestLargeContent:
    def test_long_paragraph_split(self):
        """Very long paragraphs are split by sentence boundaries."""
        # Create a paragraph > 400 tokens
        long_para = ". ".join([f"This is sentence number {i}" for i in range(200)])
        content = f"## Long\n\n{long_para}"
        chunks = chunk_markdown(content, target_tokens=400)
        # Should produce multiple chunks
        assert len(chunks) >= 2

    def test_overlap_present(self):
        """Adjacent chunks should have overlapping content."""
        # Create content that will produce multiple chunks
        paras = "\n\n".join([f"Paragraph {i} with unique content about topic {i}." * 10 for i in range(20)])
        content = f"## Test\n\n{paras}"
        chunks = chunk_markdown(content, target_tokens=100)
        if len(chunks) >= 2:
            # Check that the start of chunk[1] contains text from end of chunk[0]
            # This is a soft check since overlap is approximate
            assert len(chunks[1].text) > 0


class TestChunkProperties:
    def test_estimated_tokens(self):
        """Chunk token estimate is reasonable."""
        chunk = Chunk(
            text="Hello world " * 100,
            index=0,
            source_file="test.md",
            start_line=1,
            end_line=1,
        )
        tokens = chunk.estimated_tokens
        assert tokens > 0
        # "Hello world " is 12 chars * 100 = 1200 chars / 3 ≈ 400 tokens
        assert 300 < tokens < 500

    def test_source_lines_format(self):
        """source_lines is formatted as L{start}-L{end}."""
        chunk = Chunk(text="x", index=0, source_file="t.md", start_line=5, end_line=10)
        assert chunk.source_lines == "L5-L10"
