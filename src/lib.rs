//! CRS (coordinate reference system) definitions.
//!
//! Pulled from <https://github.com/DanielJDufour/crs-csv>.
//!
//! # Examples
//!
//! Accessing a CRS definition directly by constant:
//!
//! ```
//! let def = crs_definitions::EPSG_4326;
//!
//! assert_eq!(
//!     def.proj4,
//!     r#"+proj=longlat +datum=WGS84 +no_defs"#,
//! );
//!
//! assert_eq!(
//!     def.wkt,
//!     r#"GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]"#,
//! );
//! ```
//!
//! Lookup a CRS definition by an EPSG code:
//!
//! ```
//! # use crs_definitions::Def;
//! let def = crs_definitions::from_code(4326);
//!
//! assert_eq!(def, Some(crs_definitions::EPSG_4326));
//! ```
//!
//! Lookup a CRS definition by a constant EPSG code:
//!
//! ```
//! # use crs_definitions::Def;
//! const def: Def = crs_definitions::from_code_const::<4326>();
//!
//! assert_eq!(def, crs_definitions::EPSG_4326);
//! ```

#![no_std]

mod defs;
pub use defs::*;

mod from_code;
pub use from_code::*;

mod from_code_const;
pub use from_code_const::*;
