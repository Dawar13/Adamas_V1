def mc_spike_uart(label):
    lab = str(label).strip('"')
    for name in _NODE_ORDER:
        st = _UARTS.get(name)
        if st is None:
            print 'uart: %s %s NO WATCHER' % (lab, name)
            continue
        text = ''.join(st['tail'])
        print 'uart: %s %s len=%d' % (lab, name, len(text))
        for line in text.split('\n'):
            if line.strip():
                print 'uart: %s %s | %s' % (lab, name, line.strip())
