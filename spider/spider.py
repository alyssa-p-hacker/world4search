#!/usr/bin/python

import argparse
import json
import logging
import os
import sys
import re
import threading
import ConfigParser
import Queue
from htmlentitydefs import name2codepoint

import requests
import whoosh.analysis
import whoosh.fields
import whoosh.index


CONFIG_FILE = '/etc/world4search.conf'


def initialize_index(idir, iname, redo=False):
    """Opens the index, creating it if needed."""
    if not os.path.exists(idir):
        os.mkdir(idir)

    if redo or not whoosh.index.exists_in(idir, iname):
        schema = whoosh.fields.Schema(
            url=whoosh.fields.ID(stored=True, unique=True),
            subject=whoosh.fields.STORED,
            post=whoosh.fields.STORED,
            author=whoosh.fields.STORED,
            time=whoosh.fields.STORED,
            body=whoosh.fields.TEXT(analyzer=whoosh.analysis.StemmingAnalyzer()),
            html=whoosh.fields.STORED
        )
        ix = whoosh.index.create_in(idir, schema, indexname=iname)
        logging.info('Created new index %s in %s.', iname, idir)
    else:
        ix = whoosh.index.open_dir(idir, indexname=iname)

    return ix

def scrub(s, regices=[re.compile('<.*?>'),
                      re.compile(r'&#(\d+);'),
                      re.compile('&(%s);' % '|'.join(name2codepoint))]):
    """Turns HTML into plain text."""
    if s is None:
        return u''
    s = s.replace('<br/>', '\n')
    s = s.replace("<span class='quote'>", '> ')
    s = regices[0].sub('', s)
    s = regices[1].sub(lambda m: unichr(int(m.group(1))) \
                                 if int(m.group(1)) <= 0x10ffff \
                                 else m.group(0), s)
    s = regices[2].sub(lambda m: unichr(name2codepoint[m.group(1)]), s)
    return s

def expand_urls(html):
    """Turns relative URLs in HTML into absolute ones."""
    if not html:
        return u''
    return re.sub('href="read', 'href="%s/read' % config['url'], html)

def subject_diff(old, new):
    """
    Yields (thread, post_range, title) for each entry changed between old and new
    subject.txt.
    """
    old = set(old.splitlines(True))
    new = subject_parse(set(new.splitlines(True)) - old)
    old = subject_parse(old)
    for thread in new:
        first = 1 if thread not in old else 1 + int(old[thread]['replies'])
        last = int(new[thread]['replies'])
        yield int(thread), '%d-%d' % (first, last), new[thread]['subject']

def subject_parse(sub):
    """
    Parse subject.txt into a sequence of thread dictionaries.
    """
    regex = re.compile(u"""
        ^(?P<subject>.*)    # Subject
        <>
        .*?                 # Creator's name/???
        <>
        .*?                 # Thread icon
        <>
        (?P<id>-?\d*)       # Time posted/thread ID
        <>
        (?P<replies>\d*)    # Number of replies
        <>
        .*?                 # ???/Creator's name
        <>
        -?\d*               # Time of last post
        \\n$""", re.VERBOSE)
    ret = {}
    for line in sub:
        thread = regex.match(line.decode('utf8', 'ignore'))
        if thread is not None:
            thread = thread.groupdict()
            ret[thread['id']] = dict((a, thread[a]) for a in thread)
        else:
            logging.warning(u"[%s] Can't parse: %s",
                            config['board'], line.rstrip())
    return ret

def scrape():
    """
    Scrapes posts based on the contents of the global todo queue.
    Fetched posts are placed in the global fetched queue.
    Posts are fetched using the JSON interface over the global session.
    """
    global config, todo, fetched, session

    while not todo.empty():
        try:
            thread, posts, subject = todo.get(timeout=2)
        except:
            continue

        page = get(os.path.join(config['url'], 'json',
                                config['board'], str(thread), posts))
        if page is None:
            logging.error("Can't access %s/%d.", config['board'], thread)
            continue

        for post in page:
            page[post][u'post'] = post
            page[post][u'thread'] = thread
            page[post][u'subject'] = subject
            fetched.put(page[post])

def subject_validate(response):
    if re.match(r'^(?:.*?<>.*?<>.*?<>-?\d*<>\d*<>.*?<>-?\d*\n)+$',
                response.content) is None:
        raise ValueError('Malformed subject.txt')
    return response.content

def get(url, validate=lambda c: c.json()):
    """
    Fetches a URL and ensures the response looks right, retrying if not.
    validate should be a callable that takes a requests.models.Response and
    returns the content or raises a ValueError.
    """
    global session
    for _ in range(config['retries']):
        try:
            page = session.get(
                url,
                headers={'User-Agent': 'world4search/1.0'}
            )
            r = validate(page)
        except (requests.exceptions.RequestException, ValueError):
            pass
        else:
            return r
    return None


if __name__ == '__main__':
    global config, todo, fetched, session

    # Parse config
    cfg = ConfigParser.ConfigParser()
    try:
        with open(CONFIG_FILE) as cfgf:
            cfg.readfp(cfgf)
        config = {}
        config['index'] = cfg.get('global', 'index')
        config['boards'] = dict(cfg.items('boards'))
        config['url'] = cfg.get('global', 'boards')
        config['retries'] = cfg.getint('spider', 'retries')
        config['logfile'] = cfg.get('global', 'logfile')
    except IOError:
        print >>sys.stderr, "Can't open configuration file. Aborted."
        sys.exit(1)
    except (ConfigParser.Error, ValueError):
        print >>sys.stderr, "Can't parse configuration file. Aborted."
        sys.exit(1)

    # Logging
    logging.basicConfig(
        filename=config['logfile'], level=logging.INFO,
        format="%(asctime)s [spider] %(levelname)-8s %(message)s"
    )
    logging.getLogger('requests').setLevel(logging.ERROR)

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="world4search scraper")
    parser.add_argument('board', help='board to index')
    parser.add_argument('--redo', action='store_true',
                        help='index whole board regardless of index state')
    args = parser.parse_args()
    if args.board not in config['boards']:
        logging.warning('%s not in config.', args.board)
    config['board'] = args.board

    # Create or open index
    ix = initialize_index(config['index'], config['board'], args.redo)

    # See which posts need fetching
    old = ''
    if not args.redo:
        try:
            with open(os.path.join(config['index'],
                                   '%s.subject.txt' % config['board'])) as oldf:
                old = oldf.read()
        except IOError:
            pass

    session = requests.session()
    new = get(os.path.join(config['url'], config['board'], 'subject.txt'),
              subject_validate)
    if new is None:
        logging.error("[%s] Can't access subject.txt.", config['board'])
        sys.exit(1)

    # Queue everything up
    todo, fetched = Queue.Queue(), Queue.Queue()
    for thread in subject_diff(old, new):
        todo.put(thread)

    # Figure out how many threads to spawn and spawn them
    threads = min(todo.qsize(), min(todo.qsize(), 1000) * 31 / 1000 + 1)
    for _ in range(threads):
        threading.Thread(target=scrape).start()

    # Accumulator loop
    ixwriter = ix.writer()
    insert = ixwriter.add_document if args.redo else ixwriter.update_document
    n = 0
    while threading.activeCount() > 1 or not fetched.empty():
        try:
            post = fetched.get(timeout=2)
        except:
            continue

        try:
            post[u'now'] = int(post[u'now'])
        except (ValueError, TypeError):
            post[u'now'] = 0

        url = os.path.join(config['url'], 'read',
                           config['board'],
                           str(post[u'thread'])).decode('utf-8')
        insert(url=url,
               subject=post[u'subject'],
               post=int(post[u'post']),
               author=post[u'name'],
               time=post[u'now'],
               body=post[u'subject'] + u' ' + scrub(post[u'com']),
               html=expand_urls(post[u'com']))
        n += 1

    # Save everything
    with open(os.path.join(config['index'],
                           '%s.subject.txt' % config['board']), 'w') as newf:
        newf.write(new)
    ixwriter.commit()

    # Done
    logging.info('%d posts indexed from %s.', n, config['board'])
