# -*- coding: utf-8 -*-
"""Minimal standard XP3 extract/repack backend for MTool.

Supports common unencrypted XP3 archives with zlib-compressed index and
zlib-compressed file segments. Encrypted/custom KrkrZ archives are detected
and rejected unless an external XP3 tool is available.
"""
from __future__ import annotations
import os, struct, zlib, hashlib, subprocess, tempfile, shutil
from dataclasses import dataclass, field

SIG = b'XP3\x0d\x0a \x0a\x1a\x8bg\x01'
ENCRYPTED_FLAG = 0x80000000
# 解压上限：防解压炸弹。单条目/索引解压后的最大字节数。
MAX_UNCOMPRESSED = 512 * 1024 * 1024

@dataclass
class XP3Entry:
    name: str
    data_offset: int
    compressed_size: int
    uncompressed_size: int
    compressed: bool
    adler: int = 0
    encrypted: bool = False
    flags: int = 0

class XP3FormatError(RuntimeError): pass
class XP3EncryptedError(RuntimeError): pass

class XP3Archive:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.entries: list[XP3Entry] = []
        self._index_offset = 0
        self._index_compressed = False
        self._load()

    @property
    def encrypted(self):
        return any(e.encrypted for e in self.entries)

    def _load(self):
        try:
            self._load_inner()
        except (XP3FormatError, XP3EncryptedError):
            raise
        except (ValueError, OSError, struct.error, zlib.error) as e:
            # 统一转成受控格式错误：畸形档不再抛裸 ValueError/zlib.error/struct.error
            raise XP3FormatError(f'XP3 文件损坏: {type(e).__name__}: {e}')

    def _load_inner(self):
        with open(self.path,'rb') as f:
            if f.read(len(SIG)) != SIG:
                raise XP3FormatError('不是标准 XP3 文件')
            raw = f.read(8)
            if len(raw) != 8: raise XP3FormatError('XP3 头损坏')
            off = struct.unpack('<Q', raw)[0]
            f.seek(off)
            flag_b=f.read(1)
            if not flag_b: raise XP3FormatError('XP3 索引损坏')
            flag=flag_b[0]
            if flag == 0:
                size_raw=f.read(8)
                if len(size_raw)!=8: raise XP3FormatError('XP3 索引损坏')
                size=struct.unpack('<Q',size_raw)[0]
                if size>MAX_UNCOMPRESSED: raise XP3FormatError('索引尺寸超限（疑似解压炸弹）')
                index=f.read(size)
                if len(index)!=size: raise XP3FormatError('XP3 索引不完整')
            elif flag & 1:
                csize,usize=struct.unpack('<QQ',f.read(16))
                if usize>MAX_UNCOMPRESSED: raise XP3FormatError('索引解压后尺寸超限（疑似解压炸弹）')
                packed=f.read(csize)
                if len(packed)!=csize: raise XP3FormatError('XP3 索引不完整')
                index=zlib.decompress(packed)
                if len(index)!=usize: raise XP3FormatError('索引解压长度不匹配')
            else:
                raise XP3FormatError(f'未知 XP3 索引标志 0x{flag:02x}')
            self._index_offset=off; self._index_compressed=bool(flag&1)
            self.entries=self._parse_index(index)

    def _chunks(self, buf):
        pos=0; n=len(buf)
        while pos+12<=n:
            tag=buf[pos:pos+4]; size=struct.unpack_from('<Q',buf,pos+4)[0]; pos+=12
            end=pos+size
            if end>n: raise XP3FormatError('索引 chunk 越界')
            yield tag,buf[pos:end]
            pos=end

    def _parse_index(self,index):
        entries=[]; pending_names=[]
        # Common format stores File entries with name in info; support eliF map too.
        for tag,data in self._chunks(index):
            if tag == b'eliF':
                # Keep for compatibility: key(32) + UTF16LE name variants.
                try:
                    # Many archives use a 16-byte or 16-char key followed by UTF-16 name.
                    txt=data.decode('utf-16le',errors='ignore').rstrip('\x00')
                    if txt: pending_names.append(txt)
                except Exception: pass
                continue
            if tag != b'File':
                continue
            name=None; info_flags=0; org=arc=0; off=None; seg_org=seg_arc=0; compressed=False; adler=0
            for stag,sdata in self._chunks(data):
                if stag==b'info' and len(sdata)>=22:
                    info_flags,org,arc=struct.unpack_from('<IQQ',sdata,0)
                    nl=struct.unpack_from('<H',sdata,20)[0]
                    rawname=sdata[22:22+nl*2]
                    name=rawname.decode('utf-16le',errors='replace')
                elif stag==b'segm' and len(sdata)>=28:
                    p=0
                    flags=struct.unpack_from('<I',sdata,p)[0]; p+=4
                    # Usually one or more 28-byte segments.
                    count=(len(sdata)-4)//24
                    if count>0:
                        # first segment; merge later is handled by read_entry
                        off0,sz0,usz0=struct.unpack_from('<QQQ',sdata,p)
                        off=off0; seg_arc=sz0; seg_org=usz0; compressed=bool(flags&1)
                elif stag==b'adlr' and len(sdata)>=4:
                    adler=struct.unpack_from('<I',sdata,0)[0]
            if name is None and pending_names:
                name=pending_names.pop(0)
            if not name or off is None:
                continue
            entries.append(XP3Entry(name,off,seg_arc or arc,seg_org or org,compressed,adler,bool(info_flags & ENCRYPTED_FLAG),info_flags))
        return entries

    def extract_to(self,out_dir):
        if self.encrypted:
            raise XP3EncryptedError('检测到加密 XP3；内置后端暂不修改加密档，需使用对应外部解包器/补丁方案')
        os.makedirs(out_dir,exist_ok=True)
        out_abs=os.path.abspath(out_dir)
        with open(self.path,'rb') as f:
            for e in self.entries:
                # 防 zip-slip：档名净化，拒绝 ..、盘符、绝对路径与空段
                parts=[p for p in e.name.replace('\\','/').split('/') if p not in ('','.')]
                if not parts or any(p=='..' or (':' in p) for p in parts):
                    raise XP3FormatError(f'{e.name}: 档名含非法路径段，拒绝解包')
                f.seek(e.data_offset); blob=f.read(e.compressed_size)
                if e.uncompressed_size>MAX_UNCOMPRESSED:
                    raise XP3FormatError(f'{e.name}: 声明解压尺寸 {e.uncompressed_size:,} 超限（疑似解压炸弹）')
                try:
                    data=zlib.decompress(blob) if e.compressed else blob
                except zlib.error as zerr:
                    raise XP3FormatError(f'{e.name}: 条目数据损坏: {zerr}')
                if len(data)!=e.uncompressed_size: raise XP3FormatError(f'{e.name}: 长度不匹配')
                if e.adler and (zlib.adler32(data)&0xffffffff)!=e.adler:
                    raise XP3FormatError(f'{e.name}: Adler32 校验失败')
                dst=os.path.abspath(os.path.join(out_dir,*parts))
                if os.sep=='\\':
                    allowed=out_abs.rstrip('\\')+os.sep
                else:
                    allowed=out_abs.rstrip('/')+os.sep
                if not (dst+os.sep).startswith(allowed):
                    raise XP3FormatError(f'{e.name}: 解包路径越出输出目录，拒绝写入')
                os.makedirs(os.path.dirname(dst),exist_ok=True)
                with open(dst,'wb') as o:o.write(data)
        return len(self.entries)

    @staticmethod
    def pack_dir(src_dir,out_path,compress_level=9):
        src_dir=os.path.abspath(src_dir); out_path=os.path.abspath(out_path)
        files=[]
        for dp,_,fns in os.walk(src_dir):
            for fn in fns:
                fp=os.path.join(dp,fn)
                # 排除输出文件自身：重复打包时会把旧档塞进新档，体积滚雪球
                if os.path.abspath(fp)==out_path: continue
                rel=os.path.relpath(fp,src_dir).replace(os.sep,'/')
                files.append((rel,fp))
        files.sort(key=lambda x:x[0].lower())
        entries=[]; blobs=[]
        with open(out_path,'wb') as out:
            out.write(SIG); out.write(struct.pack('<Q',0))
            for name,fp in files:
                raw=open(fp,'rb').read(); adler=zlib.adler32(raw)&0xffffffff
                packed=zlib.compress(raw,compress_level)
                if len(packed)<len(raw): data=packed; cflag=1
                else: data=raw; cflag=0
                off=out.tell(); out.write(data)
                entries.append((name,off,len(data),len(raw),cflag,adler))
            idx=bytearray()
            for name,off,csize,usize,cflag,adler in entries:
                name_b=name.encode('utf-16le')
                info=struct.pack('<IQQH',0,usize,csize,len(name))+name_b
                seg=struct.pack('<IQQQ',cflag,off,csize,usize)
                ad=struct.pack('<I',adler)
                filedata=(b'info'+struct.pack('<Q',len(info))+info+
                          b'segm'+struct.pack('<Q',len(seg))+seg+
                          b'adlr'+struct.pack('<Q',len(ad))+ad)
                idx.extend(b'File'+struct.pack('<Q',len(filedata))+filedata)
            packed_idx=zlib.compress(bytes(idx),9)
            idx_off=out.tell()
            out.write(b'\x01'); out.write(struct.pack('<QQ',len(packed_idx),len(idx))); out.write(packed_idx)
            out.seek(len(SIG)); out.write(struct.pack('<Q',idx_off))
        return len(entries)

def find_external_tool(start_dir):
    names=('krkrxp3.exe','xp3-pack-unpack.exe','Xp3Pack.exe','xp3tool.exe')
    roots=[os.path.abspath(start_dir), os.path.dirname(os.path.abspath(__file__))]
    for root in roots:
        for n in names:
            p=os.path.join(root,n)
            if os.path.isfile(p): return p
    return None
