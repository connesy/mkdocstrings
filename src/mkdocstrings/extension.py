"""This module holds the code of the Markdown extension responsible for matching "autodoc" instructions.

The extension is composed of a Markdown [block processor](https://python-markdown.github.io/extensions/api/#blockparser)
that matches indented blocks starting with a line like `::: identifier`.

For each of these blocks, it uses a [handler][mkdocstrings.handlers.base.BaseHandler] to collect documentation about
the given identifier and render it with Jinja templates.

Both the collection and rendering process can be configured by adding YAML configuration under the "autodoc"
instruction:

```yaml
::: some.identifier
    handler: python
    options:
      option1: value1
      option2:
      - value2a
      - value2b
      option_x: etc
```
"""

from __future__ import annotations

import re
from collections import ChainMap
from typing import TYPE_CHECKING, Any, MutableSequence
from xml.etree.ElementTree import Element

import yaml
from jinja2.exceptions import TemplateNotFound
from markdown.blockprocessors import BlockProcessor
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from mkdocs.exceptions import PluginError

from mkdocstrings.handlers.base import BaseHandler, CollectionError, CollectorItem, Handlers
from mkdocstrings.loggers import get_logger

if TYPE_CHECKING:
    from markdown import Markdown
    from markdown.blockparser import BlockParser
    from mkdocs_autorefs.plugin import AutorefsPlugin


log = get_logger(__name__)


class AutoDocProcessor(BlockProcessor):
    """Our "autodoc" Markdown block processor.

    It has a [`test` method][mkdocstrings.extension.AutoDocProcessor.test] that tells if a block matches a criterion,
    and a [`run` method][mkdocstrings.extension.AutoDocProcessor.run] that processes it.

    It also has utility methods allowing to get handlers and their configuration easily, useful when processing
    a matched block.
    """

    regex = re.compile(r"^(?P<heading>#{1,6} *|)::: ?(?P<name>.+?) *$", flags=re.MULTILINE)

    def __init__(
        self,
        parser: BlockParser,
        md: Markdown,
        config: dict,
        handlers: Handlers,
        autorefs: AutorefsPlugin,
    ) -> None:
        """Initialize the object.

        Arguments:
            parser: A `markdown.blockparser.BlockParser` instance.
            md: A `markdown.Markdown` instance.
            config: The [configuration][mkdocstrings.plugin.PluginConfig] of the `mkdocstrings` plugin.
            handlers: The handlers container.
            autorefs: The autorefs plugin instance.
        """
        super().__init__(parser=parser)
        self.md = md
        self._config = config
        self._handlers = handlers
        self._autorefs = autorefs
        self._updated_envs: set = set()

    def test(self, parent: Element, block: str) -> bool:  # noqa: ARG002
        """Match our autodoc instructions.

        Arguments:
            parent: The parent element in the XML tree.
            block: The block to be tested.

        Returns:
            Whether this block should be processed or not.
        """
        return bool(self.regex.search(block))

    def run(self, parent: Element, blocks: MutableSequence[str]) -> None:
        """Run code on the matched blocks.

        The identifier and configuration lines are retrieved from a matched block
        and used to collect and render an object.

        Arguments:
            parent: The parent element in the XML tree.
            blocks: The rest of the blocks to be processed.
        """
        block = blocks.pop(0)
        match = self.regex.search(block)

        if match:
            if match.start() > 0:
                self.parser.parseBlocks(parent, [block[: match.start()]])
            # removes the first line
            block = block[match.end() :]

        block, the_rest = self.detab(block)

        if not block and blocks and blocks[0].startswith(("    handler:", "    options:")):
            # YAML options were separated from the `:::` line by a blank line.
            block = blocks.pop(0)

        if match:
            identifier = match["name"]
            heading_level = match["heading"].count("#")
            log.debug(f"Matched '::: {identifier}'")

            html, handler, data = self._process_block(identifier, block, heading_level)
            el = Element("div", {"class": "mkdocstrings"})
            # The final HTML is inserted as opaque to subsequent processing, and only revealed at the end.
            el.text = self.md.htmlStash.store(html)
            # So we need to duplicate the headings directly (and delete later), just so 'toc' can pick them up.
            headings = handler.get_headings()
            el.extend(headings)

            page = self._autorefs.current_page
            if page is not None:
                for heading in headings:
                    rendered_anchor = heading.attrib["id"]
                    self._autorefs.register_anchor(page, rendered_anchor)

                    if "data-role" in heading.attrib:
                        self._handlers.inventory.register(
                            name=rendered_anchor,
                            domain=handler.domain,
                            role=heading.attrib["data-role"],
                            priority=1,  # register with standard priority
                            uri=f"{page}#{rendered_anchor}",
                        )

                        # also register other anchors for this object in the inventory
                        try:
                            data_object = handler.collect(rendered_anchor, handler.fallback_config)
                        except CollectionError:
                            continue
                        for anchor in handler.get_anchors(data_object):
                            if anchor not in self._handlers.inventory:
                                self._handlers.inventory.register(
                                    name=anchor,
                                    domain=handler.domain,
                                    role=heading.attrib["data-role"],
                                    priority=2,  # register with lower priority
                                    uri=f"{page}#{rendered_anchor}",
                                )

            parent.append(el)

        if the_rest:
            # This block contained unindented line(s) after the first indented
            # line. Insert these lines as the first block of the master blocks
            # list for future processing.
            blocks.insert(0, the_rest)

    def _process_block(
        self,
        identifier: str,
        yaml_block: str,
        heading_level: int = 0,
    ) -> tuple[str, BaseHandler, CollectorItem]:
        """Process an autodoc block.

        Arguments:
            identifier: The identifier of the object to collect and render.
            yaml_block: The YAML configuration.
            heading_level: Suggested level of the heading to insert (0 to ignore).

        Raises:
            PluginError: When something wrong happened during collection.
            TemplateNotFound: When a template used for rendering could not be found.

        Returns:
            Rendered HTML, the handler that was used, and the collected item.
        """
        config = yaml.safe_load(yaml_block) or {}
        handler_name = self._handlers.get_handler_name(config)

        log.debug(f"Using handler '{handler_name}'")
        handler_config = self._handlers.get_handler_config(handler_name)
        handler = self._handlers.get_handler(handler_name, handler_config)

        global_options = handler_config.get("options", {})
        local_options = config.get("options", {})
        options = ChainMap(local_options, global_options)

        if heading_level:
            options = ChainMap(options, {"heading_level": heading_level})  # like setdefault

        log.debug("Collecting data")
        try:
            data: CollectorItem = handler.collect(identifier, options)
        except CollectionError as exception:
            log.error(str(exception))  # noqa: TRY400
            if PluginError is SystemExit:  # TODO: when MkDocs 1.2 is sufficiently common, this can be dropped.
                log.error(f"Error reading page '{self._autorefs.current_page}':")  # noqa: TRY400
            raise PluginError(f"Could not collect '{identifier}'") from exception

        if handler_name not in self._updated_envs:  # We haven't seen this handler before on this document.
            log.debug("Updating handler's rendering env")
            handler._update_env(self.md, self._config)
            self._updated_envs.add(handler_name)

        log.debug("Rendering templates")
        try:
            rendered = handler.render(data, options)
        except TemplateNotFound as exc:
            theme_name = self._config["theme_name"]
            log.error(  # noqa: TRY400
                f"Template '{exc.name}' not found for '{handler_name}' handler and theme '{theme_name}'.",
            )
            raise

        return rendered, handler, data


class _HeadingsPostProcessor(Treeprocessor):
    def run(self, root: Element) -> None:
        self._remove_duplicated_headings(root)

    def _remove_duplicated_headings(self, parent: Element) -> None:
        carry_text = ""
        for el in reversed(parent):  # Reversed mainly for the ability to mutate during iteration.
            if el.tag == "div" and el.get("class") == "mkdocstrings":
                # Delete the duplicated headings along with their container, but keep the text (i.e. the actual HTML).
                carry_text = (el.text or "") + carry_text
                parent.remove(el)
            else:
                if carry_text:
                    el.tail = (el.tail or "") + carry_text
                    carry_text = ""
                self._remove_duplicated_headings(el)

        if carry_text:
            parent.text = (parent.text or "") + carry_text


class _TocLabelsTreeProcessor(Treeprocessor):
    def run(self, root: Element) -> None:  # noqa: ARG002
        self._override_toc_labels(self.md.toc_tokens)  # type: ignore[attr-defined]

    def _override_toc_labels(self, tokens: list[dict[str, Any]]) -> None:
        for token in tokens:
            if (label := token.get("data-toc-label")) and token["name"] != label:
                token["name"] = label
            self._override_toc_labels(token["children"])


class MkdocstringsExtension(Extension):
    """Our Markdown extension.

    It cannot work outside of `mkdocstrings`.
    """

    def __init__(self, config: dict, handlers: Handlers, autorefs: AutorefsPlugin, **kwargs: Any) -> None:
        """Initialize the object.

        Arguments:
            config: The configuration items from `mkdocs` and `mkdocstrings` that must be passed to the block processor
                when instantiated in [`extendMarkdown`][mkdocstrings.extension.MkdocstringsExtension.extendMarkdown].
            handlers: The handlers container.
            autorefs: The autorefs plugin instance.
            **kwargs: Keyword arguments used by `markdown.extensions.Extension`.
        """
        super().__init__(**kwargs)
        self._config = config
        self._handlers = handlers
        self._autorefs = autorefs

    def extendMarkdown(self, md: Markdown) -> None:  # noqa: N802 (casing: parent method's name)
        """Register the extension.

        Add an instance of our [`AutoDocProcessor`][mkdocstrings.extension.AutoDocProcessor] to the Markdown parser.

        Arguments:
            md: A `markdown.Markdown` instance.
        """
        md.parser.blockprocessors.register(
            AutoDocProcessor(md.parser, md, self._config, self._handlers, self._autorefs),
            "mkdocstrings",
            priority=75,  # Right before markdown.blockprocessors.HashHeaderProcessor
        )
        md.treeprocessors.register(
            _HeadingsPostProcessor(md),
            "mkdocstrings_post_headings",
            priority=4,  # Right after 'toc'.
        )
        md.treeprocessors.register(
            _TocLabelsTreeProcessor(md),
            "mkdocstrings_post_toc_labels",
            priority=4,  # Right after 'toc'.
        )
