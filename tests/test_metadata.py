# pylint: disable=redefined-outer-name

from pathlib import Path

from epub_commentor.epub.metadata import MetadataField, read_metadata, write_metadata
from epub_commentor.epub.zip import Zip
from tests.utils import create_temp_dir_fixture

# 创建 metadata 专用的临时目录 fixture
metadata_temp_dir = create_temp_dir_fixture("metadata")


class TestReadMetadata:
    """测试从 EPUB 文件中读取元数据"""

    def test_read_little_prince_metadata(self, metadata_temp_dir):
        """测试读取 The little prince.epub 的元数据"""
        source_path = Path("tests/assets/The little prince.epub")
        temp_path = metadata_temp_dir / "temp_little_prince.epub"

        with Zip(source_path, temp_path) as zip_file:
            metadata, _ = read_metadata(zip_file)

            # 验证读取到的元数据数量
            assert len(metadata) == 6, "应该有 6 个可翻译的元数据字段"

            # 验证字段类型分布
            tag_names = [field.tag_name for field in metadata]
            assert "title" in tag_names
            assert "creator" in tag_names
            assert "publisher" in tag_names
            assert "description" in tag_names
            assert "subject" in tag_names

            # 验证 creator 字段出现两次
            assert tag_names.count("creator") == 2

            # 验证具体字段内容
            title_field = next(f for f in metadata if f.tag_name == "title")
            assert title_field.text == "The little prince"

            publisher_field = next(f for f in metadata if f.tag_name == "publisher")
            assert publisher_field.text == "Houghton Mifflin Harcourt"

            subject_field = next(f for f in metadata if f.tag_name == "subject")
            assert subject_field.text == "Fiction"

            # 验证 description 字段存在且有内容
            description_field = next(f for f in metadata if f.tag_name == "description")
            assert description_field.text.startswith("SUMMARY:")

    def test_read_chinese_book_metadata(self, metadata_temp_dir):
        """测试读取治疗精神病.epub 的元数据"""
        source_path = Path("tests/assets/治疗精神病.epub")
        temp_path = metadata_temp_dir / "temp_chinese.epub"

        with Zip(source_path, temp_path) as zip_file:
            metadata, _ = read_metadata(zip_file)

            # 这个文件没有可翻译的元数据字段
            assert len(metadata) == 0, "应该没有可翻译的元数据字段"

    def test_metadata_skip_fields(self, metadata_temp_dir):
        """测试验证跳过的字段类型"""
        source_path = Path("tests/assets/The little prince.epub")
        temp_path = metadata_temp_dir / "temp_skip_fields.epub"

        with Zip(source_path, temp_path) as zip_file:
            metadata, _ = read_metadata(zip_file)

            # 验证不应该出现的字段
            tag_names = [field.tag_name for field in metadata]
            assert "language" not in tag_names, "language 字段不应该被翻译"
            assert "identifier" not in tag_names, "identifier 字段不应该被翻译"
            assert "date" not in tag_names, "date 字段不应该被翻译"
            assert "meta" not in tag_names, "meta 字段不应该被翻译"
            assert "contributor" not in tag_names, "contributor 字段不应该被翻译"


class TestWriteMetadata:
    """测试写入元数据到 EPUB 文件"""

    def test_write_modified_metadata_little_prince(self, metadata_temp_dir):
        """测试修改并写回 The little prince.epub 的元数据"""
        source_path = Path("tests/assets/The little prince.epub")
        output_path = metadata_temp_dir / "modified_little_prince.epub"

        # 读取原始元数据
        with Zip(source_path, output_path) as zip_file:
            metadata, context = read_metadata(zip_file)

            # 修改所有字段
            for field in metadata:
                field.text = f"[Modified] {field.text}"

            # 写回修改后的元数据
            write_metadata(zip_file, metadata, context)

        # 验证修改是否成功
        with Zip(output_path, metadata_temp_dir / "verify_little_prince.epub") as zip_file:
            modified_metadata, _context = read_metadata(zip_file)

            assert len(modified_metadata) == len(metadata)

            # 验证所有字段都被修改
            for field in modified_metadata:
                assert field.text.startswith("[Modified] "), f"字段 {field.tag_name} 应该被修改"

            # 验证具体字段
            title_field = next(f for f in modified_metadata if f.tag_name == "title")
            assert title_field.text == "[Modified] The little prince"

            publisher_field = next(f for f in modified_metadata if f.tag_name == "publisher")
            assert publisher_field.text == "[Modified] Houghton Mifflin Harcourt"

    def test_translate_metadata_fields(self, metadata_temp_dir):
        """测试翻译元数据字段（模拟翻译场景）"""
        source_path = Path("tests/assets/The little prince.epub")
        output_path = metadata_temp_dir / "translated_little_prince.epub"

        # 读取原始元数据
        with Zip(source_path, output_path) as zip_file:
            metadata, context = read_metadata(zip_file)

            # 模拟翻译：将英文标题翻译为中文
            for field in metadata:
                if field.tag_name == "title":
                    field.text = "小王子"
                elif field.tag_name == "subject":
                    field.text = "小说"

            # 写回翻译后的元数据
            write_metadata(zip_file, metadata, context)

        # 验证翻译是否成功
        with Zip(output_path, metadata_temp_dir / "verify_translated.epub") as zip_file:
            translated_metadata, _context = read_metadata(zip_file)

            title_field = next(f for f in translated_metadata if f.tag_name == "title")
            assert title_field.text == "小王子"

            subject_field = next(f for f in translated_metadata if f.tag_name == "subject")
            assert subject_field.text == "小说"

            # 验证其他字段没有改变
            publisher_field = next(f for f in translated_metadata if f.tag_name == "publisher")
            assert publisher_field.text == "Houghton Mifflin Harcourt"

    def test_write_partial_metadata(self, metadata_temp_dir):
        """测试只修改部分元数据字段"""
        source_path = Path("tests/assets/The little prince.epub")
        output_path = metadata_temp_dir / "partial_modified.epub"

        # 读取原始元数据
        with Zip(source_path, output_path) as zip_file:
            metadata, context = read_metadata(zip_file)

            # 只修改 title 字段
            for field in metadata:
                if field.tag_name == "title":
                    field.text = "The Little Prince (Translated)"

            # 写回元数据
            write_metadata(zip_file, metadata, context)

        # 验证修改
        with Zip(output_path, metadata_temp_dir / "verify_partial.epub") as zip_file:
            modified_metadata, _context = read_metadata(zip_file)

            # 验证 title 已修改
            title_field = next(f for f in modified_metadata if f.tag_name == "title")
            assert title_field.text == "The Little Prince (Translated)"

            # 验证其他字段没有改变
            publisher_field = next(f for f in modified_metadata if f.tag_name == "publisher")
            assert publisher_field.text == "Houghton Mifflin Harcourt"

            subject_field = next(f for f in modified_metadata if f.tag_name == "subject")
            assert subject_field.text == "Fiction"


class TestMetadataFieldDataClass:
    """测试 MetadataField 数据类的功能"""

    def test_create_metadata_field(self):
        """测试创建 MetadataField 对象"""
        field = MetadataField(tag_name="title", text="Test Title")
        assert field.tag_name == "title"
        assert field.text == "Test Title"

    def test_metadata_field_equality(self):
        """测试 MetadataField 的相等性"""
        field1 = MetadataField(tag_name="title", text="Test")
        field2 = MetadataField(tag_name="title", text="Test")
        field3 = MetadataField(tag_name="title", text="Different")

        assert field1 == field2
        assert field1 != field3

    def test_metadata_field_with_special_characters(self):
        """测试包含特殊字符的元数据字段"""
        special_text = 'Title with <special> & "quotes" & 中文'
        field = MetadataField(tag_name="title", text=special_text)
        assert field.text == special_text


class TestEdgeCases:
    """测试边缘情况"""

    def test_empty_metadata_list(self, metadata_temp_dir):
        """测试空元数据列表的写入"""
        source_path = Path("tests/assets/The little prince.epub")
        output_path = metadata_temp_dir / "empty_metadata.epub"

        # 写入空元数据列表（实际上不会修改任何内容）
        with Zip(source_path, output_path) as zip_file:
            _metadata, context = read_metadata(zip_file)
            write_metadata(zip_file, [], context)

        # 验证原始元数据仍然存在
        with Zip(output_path, metadata_temp_dir / "verify_empty.epub") as zip_file:
            metadata, context = read_metadata(zip_file)
            assert len(metadata) == 6, "原始元数据应该保持不变"

    def test_metadata_with_special_characters(self, metadata_temp_dir):
        """测试包含特殊字符的元数据写入"""
        source_path = Path("tests/assets/The little prince.epub")
        output_path = metadata_temp_dir / "special_chars.epub"

        special_text = "Title <with> & \"special\" & 'chars' & 测试 & émojis 🌟"

        with Zip(source_path, output_path) as zip_file:
            metadata, context = read_metadata(zip_file)

            # 修改 title 为包含特殊字符的文本
            for field in metadata:
                if field.tag_name == "title":
                    field.text = special_text

            write_metadata(zip_file, metadata, context)

        # 验证特殊字符是否正确保存
        with Zip(output_path, metadata_temp_dir / "verify_special.epub") as zip_file:
            modified_metadata, _context = read_metadata(zip_file)
            title_field = next(f for f in modified_metadata if f.tag_name == "title")
            # XML 会转义特殊字符，但读取时会还原
            assert title_field.text == special_text

    def test_metadata_with_long_text(self, metadata_temp_dir):
        """测试包含长文本的元数据"""
        source_path = Path("tests/assets/The little prince.epub")
        output_path = metadata_temp_dir / "long_text.epub"

        long_text = "A" * 10000  # 10000 个字符的长文本

        with Zip(source_path, output_path) as zip_file:
            metadata, context = read_metadata(zip_file)

            # 修改 description 为长文本
            for field in metadata:
                if field.tag_name == "description":
                    field.text = long_text

            write_metadata(zip_file, metadata, context)

        # 验证长文本是否正确保存
        with Zip(output_path, metadata_temp_dir / "verify_long.epub") as zip_file:
            modified_metadata, _context = read_metadata(zip_file)
            description_field = next(f for f in modified_metadata if f.tag_name == "description")
            assert len(description_field.text) == 10000
            assert description_field.text == long_text

    def test_metadata_with_multiple_same_tags(self, metadata_temp_dir):
        """测试处理多个相同标签的元数据字段（如多个 creator）"""
        source_path = Path("tests/assets/The little prince.epub")
        output_path = metadata_temp_dir / "multiple_creators.epub"

        with Zip(source_path, output_path) as zip_file:
            metadata, context = read_metadata(zip_file)

            # 修改所有 creator 字段
            creator_count = 0
            for field in metadata:
                if field.tag_name == "creator":
                    creator_count += 1
                    field.text = f"Creator {creator_count} (Modified)"

            write_metadata(zip_file, metadata, context)

        # 验证所有 creator 字段都被正确修改
        with Zip(output_path, metadata_temp_dir / "verify_multiple.epub") as zip_file:
            modified_metadata, _context = read_metadata(zip_file)

            creator_fields = [f for f in modified_metadata if f.tag_name == "creator"]
            assert len(creator_fields) == 2, "应该有 2 个 creator 字段"
            assert creator_fields[0].text == "Creator 1 (Modified)"
            assert creator_fields[1].text == "Creator 2 (Modified)"


class TestRoundTrip:
    """测试完整的读写往返"""

    def test_roundtrip_metadata(self, metadata_temp_dir):
        """测试元数据的完整读写往返（不修改内容）"""
        source_path = Path("tests/assets/The little prince.epub")
        output_path = metadata_temp_dir / "roundtrip.epub"

        # 第一次读取
        with Zip(source_path, output_path) as zip_file:
            original_metadata, context = read_metadata(zip_file)
            write_metadata(zip_file, original_metadata, context)

        # 第二次读取，验证一致性
        with Zip(output_path, metadata_temp_dir / "roundtrip_verify.epub") as zip_file:
            roundtrip_metadata, _context = read_metadata(zip_file)

            assert len(roundtrip_metadata) == len(original_metadata)
            for orig, rt in zip(original_metadata, roundtrip_metadata):
                assert orig.tag_name == rt.tag_name
                assert orig.text == rt.text

    def test_multiple_roundtrips(self, metadata_temp_dir):
        """测试多次读写往返（验证稳定性）"""
        source_path = Path("tests/assets/The little prince.epub")
        current_path = source_path

        # 执行 5 次读写往返
        for i in range(5):
            output_path = metadata_temp_dir / f"roundtrip_{i}.epub"

            with Zip(current_path, output_path) as zip_file:
                metadata, context = read_metadata(zip_file)
                write_metadata(zip_file, metadata, context)

            current_path = output_path

        # 验证最终结果与原始一致
        with Zip(source_path, metadata_temp_dir / "original_temp.epub") as zip_file:
            original_metadata, context = read_metadata(zip_file)

        with Zip(current_path, metadata_temp_dir / "final_verify.epub") as zip_file:
            final_metadata, _context = read_metadata(zip_file)

        assert len(final_metadata) == len(original_metadata)
        for orig, final in zip(original_metadata, final_metadata):
            assert orig.tag_name == final.tag_name
            assert orig.text == final.text
