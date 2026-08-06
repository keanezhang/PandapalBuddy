//! pandapal-desktop/src-tauri/src/pets.rs
//!
//! 桌面宠物（Petdex 兼容）安装与枚举。
//!
//! 设计要点：
//! - 宠物本体只有两份文件：`pet.json`（元数据）+ `spritesheet.webp`（精灵图）。
//!   帧布局（192×208 / 8列×9行 / 每行一状态）不在 pet.json 里，而是渲染端约定（见前端）。
//! - Petdex 资源 URL 含内容哈希（如 kenshin-d5a6f2466786），无法从 slug 直接拼出，
//!   必须先取 `https://petdex.dev/install/<slug>` 安装脚本，从中解析两个 curl URL。
//! - 落地目录：<app_data_dir>/pets/<slug>/{pet.json, spritesheet.webp}
//!   （macOS 位于 ~/Library/Application Support/<bundle-id>/，被 fs 能力 $HOME/** 覆盖）。
//!
//! 契约：ID/来源缺失即 fail-fast（解析不到资源 URL 直接报错，绝不兜底默认 URL）。

use std::path::{Path, PathBuf};

use serde::Serialize;
use tauri::{AppHandle, Manager};

const MANIFEST_URL: &str = "https://assets.petdex.dev/manifests/petdex-v1.json";
const PETS_SUBDIR: &str = "pets";
const PET_JSON: &str = "pet.json";
const SPRITESHEET: &str = "spritesheet.webp";

/// 返回给前端的宠物快照。路径均为绝对路径，供前端 plugin-fs 读取字节渲染。
#[derive(Serialize, Clone, Debug)]
pub struct PetMeta {
    pub slug: String,
    pub display_name: String,
    pub description: String,
    /// spritesheet.webp 的绝对路径
    pub spritesheet_path: String,
    /// pet.json 的绝对路径
    pub pet_json_path: String,
}

/// pet.json 的最小结构（Petdex 官方仅这几个字段）。
#[derive(serde::Deserialize)]
struct PetJson {
    #[serde(default)]
    id: Option<String>,
    #[serde(default, rename = "displayName")]
    display_name: Option<String>,
    #[serde(default)]
    description: Option<String>,
}

/// 宠物根目录：<app_data_dir>/pets
fn pets_root(app: &AppHandle) -> Result<PathBuf, String> {
    let base = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("无法解析 app_data_dir: {}", e))?;
    Ok(base.join(PETS_SUBDIR))
}

/// 读取 <dir>/pet.json，组装 PetMeta。缺文件即返回 None（不作为合法宠物）。
fn read_pet_meta(slug: &str, dir: &Path, fallback_name: Option<&str>) -> Option<PetMeta> {
    let pet_json_path = dir.join(PET_JSON);
    let sprite_path = dir.join(SPRITESHEET);
    if !pet_json_path.is_file() || !sprite_path.is_file() {
        return None;
    }
    let raw = std::fs::read_to_string(&pet_json_path).ok()?;
    let parsed: PetJson = serde_json::from_str(&raw).ok()?;

    let display_name = parsed
        .display_name
        .filter(|s| !s.is_empty())
        .or_else(|| fallback_name.map(String::from))
        .unwrap_or_else(|| slug.to_string());
    let id = parsed.id.filter(|s| !s.is_empty()).unwrap_or_else(|| slug.to_string());

    Some(PetMeta {
        slug: id,
        display_name,
        description: parsed.description.unwrap_or_default(),
        spritesheet_path: sprite_path.to_string_lossy().to_string(),
        pet_json_path: pet_json_path.to_string_lossy().to_string(),
    })
}

/// 校验 slug 合法性（非空 + 防目录穿越）。
fn validate_slug(slug: &str) -> Result<(), String> {
    if slug.is_empty() {
        return Err("宠物标识（slug）不能为空".to_string());
    }
    if !slug.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err(format!("非法的宠物标识：{}", slug));
    }
    Ok(())
}

/// 统一构造带 Referer 的 HTTP 客户端。
fn http_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .user_agent("pandapal-desktop")
        .build()
        .map_err(|e| format!("创建 HTTP 客户端失败: {}", e))
}

/// GET 下载二进制内容（带 petdex Referer）。
async fn download(client: &reqwest::Client, url: &str, what: &str) -> Result<Vec<u8>, String> {
    let resp = client
        .get(url)
        .header("Referer", "https://petdex.dev/")
        .send()
        .await
        .map_err(|e| format!("下载{}失败: {}", what, e))?;
    if !resp.status().is_success() {
        return Err(format!("下载{}失败（HTTP {}）", what, resp.status()));
    }
    resp.bytes()
        .await
        .map(|b| b.to_vec())
        .map_err(|e| format!("读取{}内容失败: {}", what, e))
}

/// 从两个直链下载并落地一只宠物，返回元数据。安装的唯一真身。
async fn store_pet_from_urls(
    app: &AppHandle,
    client: &reqwest::Client,
    slug: &str,
    pet_json_url: &str,
    sprite_url: &str,
    fallback_name: Option<&str>,
) -> Result<PetMeta, String> {
    let pet_json_bytes = download(client, pet_json_url, "pet.json").await?;
    let sprite_bytes = download(client, sprite_url, "精灵图").await?;

    let dir = pets_root(app)?.join(slug);
    std::fs::create_dir_all(&dir).map_err(|e| format!("创建宠物目录失败: {}", e))?;
    std::fs::write(dir.join(PET_JSON), &pet_json_bytes)
        .map_err(|e| format!("写入 pet.json 失败: {}", e))?;
    std::fs::write(dir.join(SPRITESHEET), &sprite_bytes)
        .map_err(|e| format!("写入精灵图失败: {}", e))?;

    read_pet_meta(slug, &dir, fallback_name)
        .ok_or_else(|| "安装完成但读取宠物元数据失败".to_string())
}

/// 直链安装（宠物商店走这条）：用 manifest 里的资源直链下载 → 落地 → 返回元数据。
#[tauri::command]
pub async fn install_pet_urls(
    app: AppHandle,
    slug: String,
    pet_json_url: String,
    spritesheet_url: String,
    display_name: Option<String>,
) -> Result<PetMeta, String> {
    let slug = slug.trim().to_string();
    validate_slug(&slug)?;
    // 只接受 petdex 官方资源域，避免被诱导下载任意 URL
    for u in [&pet_json_url, &spritesheet_url] {
        if !u.starts_with("https://assets.petdex.dev/") {
            return Err(format!("非法的资源地址（仅允许 assets.petdex.dev）：{}", u));
        }
    }
    let client = http_client()?;
    store_pet_from_urls(&app, &client, &slug, &pet_json_url, &spritesheet_url, display_name.as_deref())
        .await
}

/// 宠物商店目录项（来自官方 manifest）。
#[derive(Serialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub struct CatalogEntry {
    pub slug: String,
    pub display_name: String,
    pub kind: String,
    pub submitted_by: String,
    pub spritesheet_url: String,
    pub pet_json_url: String,
}

/// manifest 中单个 pet 的原始结构（camelCase）。
#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct ManifestPet {
    slug: String,
    #[serde(default)]
    display_name: Option<String>,
    #[serde(default)]
    kind: Option<String>,
    #[serde(default)]
    submitted_by: Option<String>,
    spritesheet_url: String,
    pet_json_url: String,
}

#[derive(serde::Deserialize)]
struct Manifest {
    pets: Vec<ManifestPet>,
}

/// 拉取官方总清单（3700+ 只），供宠物商店浏览。一次请求拿全量。
#[tauri::command]
pub async fn fetch_pet_catalog(_app: AppHandle) -> Result<Vec<CatalogEntry>, String> {
    let client = http_client()?;
    let bytes = download(&client, MANIFEST_URL, "宠物清单").await?;
    let manifest: Manifest =
        serde_json::from_slice(&bytes).map_err(|e| format!("解析宠物清单失败: {}", e))?;
    let list = manifest
        .pets
        .into_iter()
        .map(|p| CatalogEntry {
            display_name: p.display_name.filter(|s| !s.is_empty()).unwrap_or_else(|| p.slug.clone()),
            kind: p.kind.unwrap_or_default(),
            submitted_by: p.submitted_by.unwrap_or_default(),
            spritesheet_url: p.spritesheet_url,
            pet_json_url: p.pet_json_url,
            slug: p.slug,
        })
        .collect();
    Ok(list)
}

/// 枚举已安装的宠物（扫描 <app_data_dir>/pets 下的合法子目录）。
#[tauri::command]
pub fn list_pets(app: AppHandle) -> Result<Vec<PetMeta>, String> {
    let root = pets_root(&app)?;
    if !root.is_dir() {
        return Ok(Vec::new());
    }
    let mut pets = Vec::new();
    let entries = std::fs::read_dir(&root).map_err(|e| format!("读取宠物目录失败: {}", e))?;
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let slug = entry.file_name().to_string_lossy().to_string();
        if let Some(meta) = read_pet_meta(&slug, &path, None) {
            pets.push(meta);
        }
    }
    pets.sort_by(|a, b| a.display_name.cmp(&b.display_name));
    Ok(pets)
}

/// 删除一只已安装的宠物。
#[tauri::command]
pub fn remove_pet(app: AppHandle, slug: String) -> Result<(), String> {
    if !slug.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err(format!("非法的宠物标识：{}", slug));
    }
    let dir = pets_root(&app)?.join(&slug);
    if dir.is_dir() {
        std::fs::remove_dir_all(&dir).map_err(|e| format!("删除宠物失败: {}", e))?;
    }
    Ok(())
}
