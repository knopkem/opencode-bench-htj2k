//! Thin CLI wrapper for the benchmark HTJ2K codec.
//!
//! The CLI contract is intentionally small and stable:
//!
//! ```text
//! htj2k encode IN.raw OUT.j2c --width N --height N --bit-depth B \
//!     --components C [--signed] [--decompositions N] \
//!     [--block-size WxH] [--progression LRCP|RPCL]
//! htj2k decode IN.j2c OUT.raw
//! ```
//!
//! Raw input is interleaved little-endian samples: one byte per sample for
//! bit depths up to 8, two bytes for 9..=16.  The same layout is written by
//! `decode`.

use std::env;
use std::fs;
use std::process::ExitCode;

use htj2k::codestream::Codestream;
use htj2k::file::{MemInfile, MemOutfile};
use htj2k::types::{Point, Size};

const USAGE: &str = "\
usage:
  htj2k encode INPUT.raw OUTPUT.j2c --width N --height N --bit-depth B \
--components 1 [--signed] [--decompositions N] [--block-size WxH] \
[--progression LRCP|RPCL]
  htj2k decode INPUT.j2c OUTPUT.raw

raw samples are interleaved little-endian: u8 for bit depth <= 8, u16 otherwise.
";

#[derive(Debug)]
struct EncodeArgs {
    input: String,
    output: String,
    width: u32,
    height: u32,
    bit_depth: u32,
    components: u32,
    signed: bool,
    decompositions: u32,
    block_width: u32,
    block_height: u32,
    progression: String,
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("error: {message}");
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("encode") => match parse_encode(&args[1..]) {
            Ok(options) => run_encode(options),
            Err(message) => fail(message),
        },
        Some("decode") => match parse_decode(&args[1..]) {
            Ok((input, output)) => run_decode(&input, &output),
            Err(message) => fail(message),
        },
        Some("-h" | "--help" | "help") | None => {
            print!("{USAGE}");
            ExitCode::SUCCESS
        }
        Some(other) => fail(format!("unknown command {other:?}\n{USAGE}")),
    }
}

fn parse_decode(args: &[String]) -> Result<(String, String), String> {
    if args.len() != 2 {
        return Err(format!("decode needs INPUT and OUTPUT\n{USAGE}"));
    }
    Ok((args[0].clone(), args[1].clone()))
}

fn parse_encode(args: &[String]) -> Result<EncodeArgs, String> {
    if args.len() < 2 {
        return Err(format!("encode needs INPUT and OUTPUT\n{USAGE}"));
    }
    let input = args[0].clone();
    let output = args[1].clone();
    let mut width = None;
    let mut height = None;
    let mut bit_depth = None;
    let mut components = None;
    let mut signed = false;
    let mut decompositions = 5u32;
    let mut block_width = 4u32;
    let mut block_height = 1024u32;
    let mut progression = "LRCP".to_string();

    let mut i = 2;
    while i < args.len() {
        let flag = args[i].as_str();
        let mut next = |name: &str| -> Result<String, String> {
            if i + 1 >= args.len() {
                return Err(format!("{name} needs a value"));
            }
            i += 1;
            Ok(args[i].clone())
        };
        match flag {
            "--width" => width = Some(parse_u32(&next("--width")?, "--width")?),
            "--height" => height = Some(parse_u32(&next("--height")?, "--height")?),
            "--bit-depth" => bit_depth = Some(parse_u32(&next("--bit-depth")?, "--bit-depth")?),
            "--components" => {
                components = Some(parse_u32(&next("--components")?, "--components")?)
            }
            "--signed" => signed = true,
            "--decompositions" => {
                decompositions = parse_u32(&next("--decompositions")?, "--decompositions")?
            }
            "--block-size" => {
                let value = next("--block-size")?;
                let (w, h) = value
                    .split_once('x')
                    .or_else(|| value.split_once('X'))
                    .ok_or_else(|| "--block-size must look like 64x64".to_string())?;
                block_width = parse_u32(w, "--block-size width")?;
                block_height = parse_u32(h, "--block-size height")?;
            }
            "--progression" => progression = next("--progression")?,
            other => return Err(format!("unknown option {other:?}\n{USAGE}")),
        }
        i += 1;
    }

    Ok(EncodeArgs {
        input,
        output,
        width: width.ok_or("--width is required")?,
        height: height.ok_or("--height is required")?,
        bit_depth: bit_depth.ok_or("--bit-depth is required")?,
        components: components.ok_or("--components is required")?,
        signed,
        decompositions,
        block_width,
        block_height,
        progression,
    })
}

fn parse_u32(value: &str, name: &str) -> Result<u32, String> {
    value
        .parse::<u32>()
        .map_err(|_| format!("{name} must be an integer, got {value:?}"))
}

fn bytes_per_sample(bit_depth: u32) -> usize {
    if bit_depth <= 8 {
        1
    } else {
        2
    }
}

fn sample_mask(bit_depth: u32) -> i64 {
    (1i64 << bit_depth) - 1
}

fn read_sample(bytes: &[u8], index: usize, bit_depth: u32, signed: bool) -> i32 {
    let bps = bytes_per_sample(bit_depth);
    let offset = index * bps;
    let raw = if bps == 1 {
        u32::from(bytes[offset])
    } else {
        u32::from(u16::from_le_bytes([bytes[offset], bytes[offset + 1]]))
    };
    let raw = (raw as i64 & sample_mask(bit_depth)) as i64;
    let value = if signed && (raw & (1i64 << (bit_depth - 1))) != 0 {
        raw - (1i64 << bit_depth)
    } else {
        raw
    };
    value as i32
}

fn write_sample(out: &mut Vec<u8>, value: i32, bit_depth: u32, signed: bool) {
    let (min, max) = if signed {
        (
            -(1i64 << (bit_depth - 1)),
            (1i64 << (bit_depth - 1)) - 1,
        )
    } else {
        (0, (1i64 << bit_depth) - 1)
    };
    let value = (i64::from(value)).clamp(min, max);
    if bit_depth <= 8 {
        out.push(if signed {
            (value as i8) as u8
        } else {
            value as u8
        });
    } else {
        out.extend_from_slice(&(value as i16).to_le_bytes());
    }
}

fn max_decompositions(width: u32, height: u32, requested: u32) -> u32 {
    let min_dim = width.min(height).max(1);
    let mut levels = 0u32;
    while levels < requested && (min_dim >> (levels + 1)) >= 1 {
        levels += 1;
    }
    levels.min(16)
}

fn run_encode(options: EncodeArgs) -> ExitCode {
    if options.width == 0 || options.height == 0 {
        return fail("width and height must be non-zero");
    }
    if !matches!(options.bit_depth, 1..=16) {
        return fail("--bit-depth must be in 1..=16");
    }
    if options.components != 1 {
        return fail("this benchmark grades single-component lossless HTJ2K");
    }
    if !options.block_width.is_power_of_two()
        || !options.block_height.is_power_of_two()
        || !(4..=1024).contains(&options.block_width)
        || !(4..=1024).contains(&options.block_height)
        || options.block_width * options.block_height > 4096
    {
        return fail("--block-size must be powers of two in 4..=1024 with product <= 4096");
    }

    let expected = (options.width as usize)
        .saturating_mul(options.height as usize)
        .saturating_mul(options.components as usize)
        .saturating_mul(bytes_per_sample(options.bit_depth));
    let pixels = match fs::read(&options.input) {
        Ok(data) => data,
        Err(error) => return fail(format!("read {}: {error}", options.input)),
    };
    if pixels.len() != expected {
        return fail(format!(
            "{} has {} bytes, expected {expected} for {}x{}x{} at {} bits",
            options.input,
            pixels.len(),
            options.width,
            options.height,
            options.components,
            options.bit_depth
        ));
    }

    let levels = max_decompositions(options.width, options.height, options.decompositions);
    let mut cs = Codestream::new();
    cs.access_siz_mut()
        .set_image_extent(Point::new(options.width, options.height));
    cs.access_siz_mut()
        .set_tile_size(Size::new(options.width, options.height));
    cs.access_siz_mut()
        .set_num_components(options.components);
    for component in 0..options.components {
        cs.access_siz_mut().set_comp_info(
            component,
            Point::new(1, 1),
            options.bit_depth,
            options.signed,
        );
    }
    cs.access_cod_mut().set_num_decomposition(levels);
    cs.access_cod_mut().set_reversible(true);
    cs.access_cod_mut().set_color_transform(options.components >= 3);
    cs.access_cod_mut()
        .set_block_dims(options.block_width, options.block_height);
    if let Err(error) = cs
        .access_cod_mut()
        .set_progression_order(&options.progression)
    {
        return fail(error);
    }
    cs.set_planar(0);

    let mut outfile = MemOutfile::with_capacity(pixels.len() / 4);
    if let Err(error) = cs.write_headers(&mut outfile, &[]) {
        return fail(format!("write headers: {error}"));
    }

    let width = options.width as usize;
    let components = options.components as usize;
    let mut lines: Vec<Vec<i32>> = (0..components)
        .map(|_| vec![0i32; width])
        .collect();
    for y in 0..options.height as usize {
        let row = y * width;
        for x in 0..width {
            for component in 0..components {
                lines[component][x] = read_sample(
                    &pixels,
                    (row + x) * components + component,
                    options.bit_depth,
                    options.signed,
                );
            }
        }
        for (component, line) in lines.iter().enumerate() {
            if let Err(error) = cs.exchange(line, component as u32) {
                return fail(format!("encode exchange: {error}"));
            }
        }
    }
    if let Err(error) = cs.flush(&mut outfile) {
        return fail(format!("encode flush: {error}"));
    }
    if let Err(error) = fs::write(&options.output, outfile.get_data()) {
        return fail(format!("write {}: {error}", options.output));
    }
    ExitCode::SUCCESS
}

fn run_decode(input: &str, output: &str) -> ExitCode {
    let data = match fs::read(input) {
        Ok(data) => data,
        Err(error) => return fail(format!("read {input}: {error}")),
    };
    let mut infile = MemInfile::new(&data);
    let mut cs = Codestream::new();
    if let Err(error) = cs.read_headers(&mut infile) {
        return fail(format!("read headers: {error}"));
    }
    let width = cs.access_siz().get_image_extent().x;
    let height = cs.access_siz().get_image_extent().y;
    let components = cs.access_siz().get_num_components();
    if components == 0 || width == 0 || height == 0 {
        return fail("codestream has an empty image");
    }
    let bit_depth = u32::from(cs.access_siz().get_bit_depth(0) as u8);
    let signed = cs.access_siz().is_signed(0);
    if let Err(error) = cs.create(&mut infile) {
        return fail(format!("decode create: {error}"));
    }

    let bytes = (width as usize)
        .saturating_mul(height as usize)
        .saturating_mul(usize::from(components))
        .saturating_mul(bytes_per_sample(bit_depth));
    let mut out = Vec::with_capacity(bytes);
    for _ in 0..height {
        let mut lines: Vec<Vec<i32>> = Vec::with_capacity(usize::from(components));
        for component in 0..u32::from(components) {
            match cs.pull(component) {
                Some(line) => {
                    if line.len() != width as usize {
                        return fail("decoded line width mismatch");
                    }
                    lines.push(line);
                }
                None => return fail("decoder returned no line"),
            }
        }
        for x in 0..width as usize {
            for line in &lines {
                write_sample(&mut out, line[x], bit_depth, signed);
            }
        }
    }
    if let Err(error) = fs::write(output, out) {
        return fail(format!("write {output}: {error}"));
    }
    ExitCode::SUCCESS
}
