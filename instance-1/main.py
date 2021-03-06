from logging import debug, error, info
from os import environ
from cgi import parse_qs
from hashlib import md5
from random import sample
from struct import pack
from google.appengine.api.memcache import get, set as mset, get_multi, delete as mdel, incr, decr

"""
A ntrack tracker
================

http://repo.cat-v.org/atrack/

Memcached namespaces:

- 'T': Keys / info_hashes -> 'Compact' style string of binary encoded ip+ports 
- 'K': Keys / info_hashes -> String of | delimited peer-hashes DEPRECATED
- 'I': peer-hash -> Metadata string: 'ip|port' DEPRECATED
- 'P': peer-hash -> Anything 'true'. TODO: Should be a 'ref count'.
- 'S': "%s!%s" (Keys/info_hash, param) -> Integer
- 'D': Debug data

A peer hash is: md5("%s/%d" % (ip, port)).hexdigest()[:16]

This allows peer info to be shared and decay by itself, we will delete
references to peer from the key namespace lazily.
"""

STATS=True # Set to false if you don't want to keep track of the number of seeders and leechers
ERRORS=True # If false we don't bother report errors to clients to save(?) bandwith and CPU
INTERVAL=10800
MEMEXPIRE=60*60*24*2 # When to expire peers from memcache?

def resps(s):
    print "Content-type: text/plain"
    print ""
    print s, # Make sure we don't add a trailing new line!

def prof_main():
    # This is the main function for profiling 
    import cProfile, pstats, StringIO
    import logging
    prof = cProfile.Profile()
    prof = prof.runctx("real_main()", globals(), locals())
    stream = StringIO.StringIO()
    stats = pstats.Stats(prof, stream=stream)
    stats.sort_stats("time")  # Or cumulative
    stats.print_stats(80)  # 80 = how many to print
    # The rest is optional.
    stats.print_callees()
    stats.print_callers()
    logging.info("Profile data:\n%s", stream.getvalue())


def real_main():
    args = parse_qs(environ['QUERY_STRING'])

    if not args:
        print "Status: 301 Moved Permanantly\nLocation: /\n\n",
        return

    for a in ('info_hash', 'port'):
        if a not in args or len(args[a]) != 1:
            if ERRORS:
                resps(bencode({'failure reason': "You must provide %s!"%a}))
            return

    key = args['info_hash'][0]
    if STATS:
        key_complete = '%s!complete'%key
        key_incomplete = '%s!incomplete'%key
    left = args.pop('left', [None])[0]
    err = None

    if(len(key) > 128):
        err = "Insanely long key!"
    else:
        try:
            port = int(args['port'][0])
            if port > 65535 or port < 1:
                err = "Invalid port number!"

        except:
            err = "Invalid port number!"

    if err:
        if ERRORS:
            resps(bencode({'failure reason': err}))
        return

    # Crop raises chance of a clash, plausible deniability for the win!
    #phash = md5("%s/%d" % (ip, port)).hexdigest()[:16]  # XXX TODO Instead of a hash, we should use the packed ip+port
    i = environ['REMOTE_ADDR'].split('.') # TODO Check that it is an v4 address
    phash = pack('>4BH', int(i[0]), int(i[1]), int(i[2]), int(i[3]), port)
    # TODO BT: If left=0, the download is done and we should not return any peers.
    event = args.pop('event', [None])[0]
    if event == 'stopped':
        # Maybe we should only remove it from this track, but this is good enough.
        mdel(phash, namespace='P')
        if STATS:
            # XXX Danger of incomplete underflow!
            if left == '0':
                decr(key_complete, namespace='S')
            else:
                decr(key_incomplete, namespace='S')

        return # They are going away, don't waste bw/cpu on this.
        resps(bencode({'interval': INTERVAL, 'peers': []}))

    elif STATS and event == 'completed':
        decr(key_incomplete, namespace='S')
        incr(key_complete, namespace='S')

    updatetrack = False

    # Get existing peers

    PEER_SIZE = 6
    MAX_PEERS = 32
    MAX_PEERS_SIZE = MAX_PEERS*PEER_SIZE
    a = get(key, namespace='T')
    # TODO: perhaps we should use the array module: http://docs.python.org/library/array.html

    if a:
        als = [a[x:x+PEER_SIZE] for x in xrange(0, l, PEER_SIZE)]
        l = len(als)
        if l > MAX_PEERS:
            i = randrange(0, l-MAX_PEERS)
            ii = i*PEER_SIZE
            rs = a[ii:ii+MAX_PEERS_SIZE]
            rls = als[i:i+MAX_PEERS]
        else:
            rs = a
            rls = als

        rrls = get_multi(rls, namespace='P').keys()

        # NOTE Do not use a generator, generators are always true even if empty!
        lostpeers = [p for p in rls if p not in rrls] 
        if lostpeers: # Remove lost peers
            rs = ''.join(rrls)

            [als.remove(p) for p in lostpeers if p in als]
            a = ''.join(als)

            updatetrack = True
            if STATS:
                # XXX medecau suggests we might use len(s) instead of counting leechers.
                # XXX If we underflow, should decrement from '!complete'
                decr(key_incomplete, len(lostpeers), namespace='S') 

        # Remove self from returned peers
        # XXX Commented out as we are shorter on CPU than bw
        #if phash in peers:
        #    peers.pop(phash, None) 

    # New track!
    else:
        a = rs = ''
        als = []
        if STATS:
            mset(key_complete, '0', namespace='S')
            mset(key_incomplete, '0', namespace='S')

    if phash not in als: # Assume new peer
        # XXX We don't refresh the peers expiration date on every request!
        mset(phash, 1, namespace='P') 
        a += phash
        updatetrack = True
        if STATS: # Should we bother to check event == 'started'? Why?
            if left == '0':
                incr(key_complete, namespace='S')
            else:
                incr(key_incomplete, namespace='S')

    if updatetrack:
        mset(key, a, namespace='K')

    if STATS:
        resps(bencode({'interval':INTERVAL, 'peers':rs,
            'complete':(get(key_complete, namespace='S') or 0),
            'incomplete':(get(key_incomplete, namespace='S') or 0)}))
    else:
        resps(bencode({'interval':INTERVAL, 'peers':rs}))


#main = prof_main
main = real_main

################################################################################
# Bencode encoding code by Petru Paler, slightly simplified by uriel
from types import StringType, IntType, LongType, DictType, ListType, TupleType, BooleanType

#class Bencached(object):
#    __slots__ = ['bencoded']
#
#    def __init__(self, s):
#        self.bencoded = s

#def encode_bencached(x,r):
#    r.append(x.bencoded)

def encode_int(x, r):
    r.extend(('i', str(x), 'e'))

def encode_string(x, r):
    r.extend((str(len(x)), ':', x))

def encode_list(x, r):
    r.append('l')
    for i in x:
        encode_func[type(i)](i, r)
    r.append('e')

def encode_dict(x,r):
    r.append('d')
    ilist = x.items()
    ilist.sort()
    for k, v in ilist:
        r.extend((str(len(k)), ':', k))
        encode_func[type(v)](v, r)
    r.append('e')

encode_func = {}
#encode_func[type(Bencached(0))] = encode_bencached
encode_func[IntType] = encode_int
encode_func[LongType] = encode_int
encode_func[StringType] = encode_string
encode_func[ListType] = encode_list
encode_func[TupleType] = encode_list
encode_func[DictType] = encode_dict
encode_func[BooleanType] = encode_int

def bencode(x):
    r = []
    encode_func[type(x)](x, r)
    return ''.join(r)

def test_bencode():
    assert bencode(4) == 'i4e'
    assert bencode(0) == 'i0e'
    assert bencode(-10) == 'i-10e'
    assert bencode(12345678901234567890L) == 'i12345678901234567890e'
    assert bencode('') == '0:'
    assert bencode('abc') == '3:abc'
    assert bencode('1234567890') == '10:1234567890'
    assert bencode([]) == 'le'
    assert bencode([1, 2, 3]) == 'li1ei2ei3ee'
    assert bencode([['Alice', 'Bob'], [2, 3]]) == 'll5:Alice3:Bobeli2ei3eee'
    assert bencode({}) == 'de'
    assert bencode({'age': 25, 'eyes': 'blue'}) == 'd3:agei25e4:eyes4:bluee'
    assert bencode({'spam.mp3': {'author': 'Alice', 'length': 100000}}) == 'd8:spam.mp3d6:author5:Alice6:lengthi100000eee'
    #assert bencode(Bencached(bencode(3))) == 'i3e'
    try:
        bencode({1: 'foo'})
    except TypeError:
        return
    assert 0

################################################################################

if __name__ == '__main__':
    main()
