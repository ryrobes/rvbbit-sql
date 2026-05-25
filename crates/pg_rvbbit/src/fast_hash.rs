use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasherDefault, Hasher};

pub(crate) type FastHashMap<K, V> = HashMap<K, V, BuildHasherDefault<FastHasher>>;
pub(crate) type FastHashSet<T> = HashSet<T, BuildHasherDefault<FastHasher>>;

#[derive(Default)]
pub(crate) struct FastHasher {
    hash: u64,
}

impl FastHasher {
    #[inline]
    fn add_u64(&mut self, value: u64) {
        const K: u64 = 0x517c_c1b7_2722_0a95;
        self.hash = self.hash.rotate_left(5) ^ value;
        self.hash = self.hash.wrapping_mul(K);
    }
}

impl Hasher for FastHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.hash
    }

    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut chunks = bytes.chunks_exact(8);
        for chunk in &mut chunks {
            self.add_u64(u64::from_ne_bytes(chunk.try_into().expect("8-byte chunk")));
        }

        let remainder = chunks.remainder();
        if !remainder.is_empty() {
            let mut tail = [0u8; 8];
            tail[..remainder.len()].copy_from_slice(remainder);
            self.add_u64(u64::from_ne_bytes(tail) ^ remainder.len() as u64);
        }
    }

    #[inline]
    fn write_u8(&mut self, i: u8) {
        self.add_u64(i as u64);
    }

    #[inline]
    fn write_u16(&mut self, i: u16) {
        self.add_u64(i as u64);
    }

    #[inline]
    fn write_u32(&mut self, i: u32) {
        self.add_u64(i as u64);
    }

    #[inline]
    fn write_u64(&mut self, i: u64) {
        self.add_u64(i);
    }

    #[inline]
    fn write_u128(&mut self, i: u128) {
        self.add_u64(i as u64);
        self.add_u64((i >> 64) as u64);
    }

    #[inline]
    fn write_usize(&mut self, i: usize) {
        self.add_u64(i as u64);
    }

    #[inline]
    fn write_i8(&mut self, i: i8) {
        self.add_u64(i as u8 as u64);
    }

    #[inline]
    fn write_i16(&mut self, i: i16) {
        self.add_u64(i as u16 as u64);
    }

    #[inline]
    fn write_i32(&mut self, i: i32) {
        self.add_u64(i as u32 as u64);
    }

    #[inline]
    fn write_i64(&mut self, i: i64) {
        self.add_u64(i as u64);
    }

    #[inline]
    fn write_i128(&mut self, i: i128) {
        self.write_u128(i as u128);
    }

    #[inline]
    fn write_isize(&mut self, i: isize) {
        self.add_u64(i as u64);
    }
}
