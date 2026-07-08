DOCKER=docker
IMGTAG=wisefood/recipe-wrangler:patch

.PHONY: all build push

all: build push

build:
	$(DOCKER) build . -t $(IMGTAG)

push:
	$(DOCKER) push $(IMGTAG)
