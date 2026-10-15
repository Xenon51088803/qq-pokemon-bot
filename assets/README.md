# 全国图鉴资源

范围为全国编号 #001～#1025，共 1025 种默认形态，按引入世代划分 1～9 代。地区形态、Mega、异色等不另计收集种类。`pokemon.json` 为名称、别名和世代；`sprites/001.png` 至 `sprites/1025.png` 为本地图片，由 `scripts/prepare_assets.py` 整理。

`profiles.json` 提供全国 1025 种的分类、属性、身高（米）、体重（千克）和中文介绍。由 `scripts/prepare_profiles.py` 从 PokeAPI CSV 整理；缺少中文介绍或分类的种类，使用[宝可梦中国官网](https://www.pokemon.cn/play/pokedex/)对应编号默认形态页面的第一段介绍，不将翻译或生成内容冒充官方原文。

来源与校验值见 `profiles.sources.json`。PokeAPI 介绍取语言 12 的最大可用游戏版本编号，并在 `description_version_id` 中保留编号；官网来源的该字段为 null，`description_source` 记录页面 URL，`source_sha256` 保留页面 SHA-256。清除原文空白和换行以便排版。属性采用普通形态的现行属性，包含妖精等后续调整。

资料整理使用 `pokemon.csv`、`pokemon_types.csv`、`type_names.csv`、`pokemon_species_names.csv` 和 `pokemon_species_flavor_text.csv`。“更新资源.cmd”会自动补齐缺图与未准备的全国资料；`scripts/prepare_assets.py --refresh-names` 刷新名称，`--refresh-profiles` 刷新介绍及属性，`scripts/prepare_profiles.py` 可单独整理介绍。原始 CSV、官网名称索引和介绍页面缓存在 `runtime/resource-cache`；重复整理复用缓存，刷新参数重新下载。缺少或不完整的中文介绍会使整理报错，不以空文本覆盖现有介绍文件。资料工具不读取或修改训练家数据。

- 简体中文名称：PokeAPI 的 `pokemon_species_names.csv`（语言 12），并用[官网索引](https://www.pokemon.cn/play/pokedex/api/v1)校正简体字；旧上游名称和已有别名保留为查询别名。
- 默认正面像素图片：<https://github.com/PokeAPI/sprites> 及 <https://play.pokemonshowdown.com/sprites/gen5/>；具体每张图片的实际下载地址见 `sources.json` 的 `source_by_file`。
- 上游数据仓库：<https://github.com/PokeAPI/pokeapi>。
- 名称默认通过 jsDelivr Fastly 的公开仓库镜像下载。图片默认使用 Pokemon Showdown 的静态正面像素图；`--direct` 可改用 GitHub。已有可用图片不会重复下载。
- `sources.json` 记录下载来源、整理时间、文件 SHA-256 及缺失项。

图片与宝可梦名称的相关权利归各权利人所有。公开可下载不代表取得使用授权；非商业使用也不自动免责。正式公开运营前应确认素材使用条件。此项目仅整理资源，不宣称拥有这些素材的版权。

运行时完全使用本地资源，不请求 PokeAPI。资源文件丢失或损坏时显示占位；中文资料文件缺失时会提示重新整理资料。
