CC     ?= gcc
CFLAGS ?= -O3 -fno-strict-aliasing

mackengine: engine.c
	$(CC) $(CFLAGS) -o $@ engine.c -lm

clean:
	rm -f mackengine
