from pathlib import Path
from upload_file_to_mineru import MineruConfig, MineruPARSE

config = MineruConfig(
input_path=Path(r"E:\docs\pdf"), # 目录或单个文件
file_suffixes={".pdf"},
)
manifest = MineruPARSE(config).run() # 阻塞直至全部完成