"""
OpenClaw Memory System v2 — Markdown-Aware Chunker
====================================================
Splits Markdown files into semantically coherent chunks for indexing.

Strategy:
  1. Split by ## headings into sections
  2. Within sections, split by paragraphs (double newline)
  3. Merge adjacent paragraphs up to ~400 tokens
  4. If single paragraph > 400 tokens, split by sentence boundaries
  5. Adjacent chunks overlap by ~80 tokens
  6. Each chunk keeps its parent ## heading as context prefix
  
Special handling:
  - Code blocks (```...```): kept whole, never split
  - Tables: kept whole
  - Lists: same-level items kept together when possible
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import CHUNK_TARGET_TOKENS, CHUNK_OVERLAP_TOKENS

logger = logging.getLogger(__name__)

# Rough token estimate: 1 token ≈ 4 chars for English, ~1.5 for CJK
# Use a conservative 3 chars/token for mixed content
CHARS_PER_TOKEN = 3


@dataclass
class Chunk:
    """A single chunk of text with metadata."""
    text: str
    index: int
    source_file: str
    start_line: int
    end_line: int
    heading: str = ""  # Parent ## heading

    @property
    def estimated_tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN

    @property
    def source_lines(self) -> str:
        return f"L{self.start_line}-L{self.end_line}"


def chunk_markdown(
    content: str,
    source_file: str = "",
    target_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[Chunk]:
    """
    Split a Markdown document into semantically coherent chunks.
    
    Args:
        content: Full Markdown content.
        source_file: File path for metadata.
        target_tokens: Target tokens per chunk (~400).
        overlap_tokens: Overlap tokens between adjacent chunks (~80).
    
    Returns:
        List of Chunk objects.
    """
    if not content or not content.strip():
        return []

    target_chars = target_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN

    # Step 1: Split into sections by ## headings
    sections = _split_by_headings(content)

    # Step 2: Process each section into chunks
    all_chunks = []
    chunk_index = 0

    for heading, section_text, section_start_line in sections:
        # Step 2a: Extract special blocks (code, tables)
        blocks = _split_into_blocks(section_text, section_start_line)

        # Step 2b: Merge small blocks, split large ones
        merged = _merge_blocks(blocks, target_chars)

        # Step 2c: Apply overlap
        overlapped = _apply_overlap(merged, overlap_chars)

        for text, start_line, end_line in overlapped:
            # Prepend heading context
            if heading:
                prefixed_text = f"## {heading}\n\n{text}"
            else:
                prefixed_text = text

            chunk = Chunk(
                text=prefixed_text.strip(),
                index=chunk_index,
                source_file=source_file,
                start_line=start_line,
                end_line=end_line,
                heading=heading,
            )
            all_chunks.append(chunk)
            chunk_index += 1

    logger.debug(
        "Chunked %s: %d chunks from %d chars",
        source_file, len(all_chunks), len(content),
    )
    return all_chunks


def _split_by_headings(content: str) -> list[tuple[str, str, int]]:
    """
    Split content by ## headings.
    Returns: [(heading_text, section_content, start_line), ...]
    """
    lines = content.split("\n")
    sections: list[tuple[str, str, int]] = []
    current_heading = ""
    current_lines: list[str] = []
    current_start = 1

    for i, line in enumerate(lines, 1):
        # Match ## headings (not # or ###)
        heading_match = re.match(r"^##\s+(.+)$", line)
        if heading_match:
            # Save previous section
            if current_lines or not sections:
                text = "\n".join(current_lines)
                if text.strip():
                    sections.append((current_heading, text, current_start))

            current_heading = heading_match.group(1).strip()
            current_lines = []
            current_start = i + 1
        else:
            current_lines.append(line)

    # Don't forget the last section
    text = "\n".join(current_lines)
    if text.strip():
        sections.append((current_heading, text, current_start))

    return sections


@dataclass
class _Block:
    """Internal block: a paragraph, code block, table, or list."""
    text: str
    start_line: int
    end_line: int
    block_type: str = "paragraph"  # paragraph, code, table, list


def _split_into_blocks(text: str, base_line: int) -> list[_Block]:
    """
    Split section text into blocks, preserving code blocks, tables, and lists.
    """
    lines = text.split("\n")
    blocks: list[_Block] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Code block (```)
        if line.strip().startswith("```"):
            block_start = i
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                i += 1
            i += 1  # Skip closing ```
            block_text = "\n".join(lines[block_start:i])
            blocks.append(_Block(
                text=block_text,
                start_line=base_line + block_start,
                end_line=base_line + i - 1,
                block_type="code",
            ))
            continue

        # Table (lines starting with |)
        if line.strip().startswith("|"):
            block_start = i
            while i < len(lines) and lines[i].strip().startswith("|"):
                i += 1
            block_text = "\n".join(lines[block_start:i])
            blocks.append(_Block(
                text=block_text,
                start_line=base_line + block_start,
                end_line=base_line + i - 1,
                block_type="table",
            ))
            continue

        # List items (-, *, 1.)
        if re.match(r"^\s*[-*]\s|^\s*\d+\.\s", line):
            block_start = i
            while i < len(lines) and (
                re.match(r"^\s*[-*]\s|^\s*\d+\.\s", lines[i])
                or (lines[i].strip() and lines[i].startswith("  "))
            ):
                i += 1
            block_text = "\n".join(lines[block_start:i])
            blocks.append(_Block(
                text=block_text,
                start_line=base_line + block_start,
                end_line=base_line + i - 1,
                block_type="list",
            ))
            continue

        # Empty line — skip
        if not line.strip():
            i += 1
            continue

        # Regular paragraph — read until empty line or special block
        block_start = i
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip():
                break
            if next_line.strip().startswith("```"):
                break
            if next_line.strip().startswith("|") and i > block_start:
                break
            if re.match(r"^\s*[-*]\s|^\s*\d+\.\s", next_line) and i > block_start:
                break
            i += 1

        block_text = "\n".join(lines[block_start:i])
        if block_text.strip():
            blocks.append(_Block(
                text=block_text,
                start_line=base_line + block_start,
                end_line=base_line + i - 1,
                block_type="paragraph",
            ))

    return blocks


def _merge_blocks(
    blocks: list[_Block],
    target_chars: int,
) -> list[tuple[str, int, int]]:
    """
    Merge small adjacent blocks up to target_chars.
    Code and table blocks are never merged with others.
    Returns: [(text, start_line, end_line), ...]
    """
    if not blocks:
        return []

    result: list[tuple[str, int, int]] = []
    buffer_text = ""
    buffer_start = blocks[0].start_line
    buffer_end = blocks[0].start_line

    for block in blocks:
        # Special blocks: emit buffer first, then emit block alone
        if block.block_type in ("code", "table"):
            if buffer_text.strip():
                result.append((buffer_text.strip(), buffer_start, buffer_end))
            result.append((block.text, block.start_line, block.end_line))
            buffer_text = ""
            buffer_start = block.end_line + 1
            buffer_end = block.end_line + 1
            continue

        # Check if adding this block would exceed target
        candidate = (buffer_text + "\n\n" + block.text).strip() if buffer_text else block.text
        if len(candidate) > target_chars and buffer_text.strip():
            # Emit current buffer
            result.append((buffer_text.strip(), buffer_start, buffer_end))
            buffer_text = block.text
            buffer_start = block.start_line
            buffer_end = block.end_line
        else:
            if not buffer_text:
                buffer_start = block.start_line
            buffer_text = candidate
            buffer_end = block.end_line

    # Emit remaining buffer
    if buffer_text.strip():
        result.append((buffer_text.strip(), buffer_start, buffer_end))

    # Split any chunks that are still too large
    final = []
    for text, start, end in result:
        if len(text) > target_chars * 1.5:
            # Split by sentence boundaries
            sentences = _split_sentences(text)
            sub_chunks = _merge_sentences(sentences, target_chars, start, end)
            final.extend(sub_chunks)
        else:
            final.append((text, start, end))

    return final


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences (rough heuristic)."""
    # Split on period/question/exclamation followed by space or newline
    # Also split on Chinese sentence endings
    parts = re.split(r'(?<=[.!?。！？\n])\s+', text)
    return [p for p in parts if p.strip()]


def _merge_sentences(
    sentences: list[str],
    target_chars: int,
    start_line: int,
    end_line: int,
) -> list[tuple[str, int, int]]:
    """Merge sentences up to target size."""
    if not sentences:
        return []

    result = []
    buffer = ""
    total_lines = end_line - start_line + 1
    lines_per_sentence = max(1, total_lines // len(sentences))

    for i, sentence in enumerate(sentences):
        candidate = (buffer + " " + sentence).strip() if buffer else sentence
        if len(candidate) > target_chars and buffer:
            est_start = start_line + (i - 1) * lines_per_sentence
            est_end = start_line + i * lines_per_sentence
            result.append((buffer.strip(), est_start, est_end))
            buffer = sentence
        else:
            buffer = candidate

    if buffer.strip():
        result.append((buffer.strip(), start_line, end_line))

    return result


def _apply_overlap(
    chunks: list[tuple[str, int, int]],
    overlap_chars: int,
) -> list[tuple[str, int, int]]:
    """
    Add overlap between adjacent chunks.
    Takes the last overlap_chars from the previous chunk and prepends to the next.
    """
    if len(chunks) <= 1 or overlap_chars <= 0:
        return chunks

    result = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_text, _, _ = chunks[i - 1]
        curr_text, curr_start, curr_end = chunks[i]

        # Take the tail of the previous chunk as overlap
        if len(prev_text) > overlap_chars:
            # Try to break at a word boundary
            overlap_text = prev_text[-overlap_chars:]
            space_idx = overlap_text.find(" ")
            if space_idx > 0:
                overlap_text = overlap_text[space_idx + 1:]
            combined = overlap_text + "\n\n" + curr_text
        else:
            combined = curr_text

        result.append((combined, curr_start, curr_end))

    return result
