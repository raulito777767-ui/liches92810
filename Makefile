CC     ?= gcc
CFLAGS ?= -O3 -fno-strict-aliasing

mackengine: engine.c
	$(CC) $(CFLAGS) -o $@ engine.c -lm

bench: mackengine
	./mackengine bench 12

clean:
	rm -f mackengine
