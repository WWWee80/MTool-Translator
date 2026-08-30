"""Pure-Python fallback for SExtractor's Okumura LZSS extension."""
N=4096; F=18; THRESHOLD=2; RSTART=N-F
def decompress(dst, src):
    src=bytes(src)
    if not src: return 0
    text_buf=bytearray(N+F-1)
    r=RSTART; si=0; di=0; flags=0; dlen=len(dst)
    while True:
        flags >>= 1
        if (flags & 0x100)==0:
            if si>=len(src): break
            flags=src[si] | 0xFF00; si+=1
        if flags & 1:
            if si>=len(src) or di>=dlen: break
            c=src[si]; si+=1
            dst[di]=c; di+=1; text_buf[r]=c; r=(r+1)&(N-1)
        else:
            if si+1>=len(src): break
            i=src[si]; j=src[si+1]; si+=2
            i |= (j & 0xF0)<<4; count=(j & 0x0F)+THRESHOLD+1
            for k in range(count):
                if di>=dlen: return di
                c=text_buf[(i+k)&(N-1)]
                dst[di]=c; di+=1; text_buf[r]=c; r=(r+1)&(N-1)
    return di
def _find_match(data,pos,index,limit=64):
    if pos+3>len(data): return 0,0
    key=data[pos:pos+3]; best_j=0; best_k=0
    for j in reversed(index.get(key, ())[-limit:]):
        if j>=pos or pos-j>N: continue
        maxlen=min(F,len(data)-pos); k=0
        while k<maxlen:
            sj=j+k
            if sj < pos:
                if data[sj]!=data[pos+k]: break
            else:
                if data[pos+(k-(pos-j))] != data[pos+k] if pos-j>0 else False: break
            k+=1
        if k>best_k:
            best_j,best_k=j,k
            if k==F: break
    return best_j,best_k
def compress(dst,src):
    data=bytes(src)
    if not data: return 0
    out=bytearray(); index={}; pos=0
    while pos<len(data):
        flag_pos=len(out); out.append(0); payload=bytearray(); mask=1
        for _ in range(8):
            if pos>=len(data): break
            j,k=_find_match(data,pos,index)
            if k>=THRESHOLD+1:
                ring=(RSTART+j)&(N-1)
                payload.append(ring&0xFF)
                payload.append(((ring>>4)&0xF0)|(k-(THRESHOLD+1)))
                for q in range(k):
                    p=pos+q
                    if p+3<=len(data): index.setdefault(data[p:p+3],[]).append(p)
                pos+=k
            else:
                out[flag_pos]|=mask; payload.append(data[pos])
                if pos+3<=len(data): index.setdefault(data[pos:pos+3],[]).append(pos)
                pos+=1
            mask <<= 1
        out.extend(payload)
    if len(out)>len(dst): raise BufferError("LZSS output buffer too small")
    dst[:len(out)]=out; return len(out)
