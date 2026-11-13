# Source and scope

Community skill for Wingman AI; not an official ShipBit or Cloud Imperium product.

The public v3 lifecycle, screenshot and AI pattern follows
ShipBit/wingman-ai Proactive Comments and Vision AI, reviewed at commit
`f0364106a9ea13eb5aee45f73cd9af86c79cd213`. Absolute screenshot-region conventions
also follow its Auto Screenshot helper. Wingman source remains maintained by
its authors; no Core files are patched or included here.
Warnings use Core's BACKGROUND speech priority so a busy reply does not queue
an obsolete notice. The current priority semantics were reviewed at the same pin.

The default image profile still delegates Wingman's original encoder. The
optional high-detail profile uses Pillow, already supplied by Wingman, and the
same current Core thread-priority helper. No Pillow or Core implementation is
copied into the skill. Its profile dimensions follow Anthropic's documented
Haiku 5.5 image tier, not a claim about every provider's supported resolution.

The optional reader calls the public v3 secret facade and standard
OpenAI-compatible HTTP protocol. Its bounded streaming, cancellation and
credential-handling design reuses this project's GPL-3.0 transport methods;
the standalone package does not include the companion/domain transport stack.
HTTPX is already supplied by Wingman's OpenAI dependency and is not bundled.
No private Wingman provider instance or Core patch is used. Supported JSON and
reasoning controls are provider/model dependent and remain explicit settings.

Deterministic warning logic adapts this project's alert/shield transition ideas
to passive visual claims. Native input, source qualification and active-control
components are deliberately not bundled. The included logo is an original
geometric warning mark, not a supplier logo or character likeness.

See LICENSE for the source licence. No game images, credentials, audio tracks,
voice assets or model weights are distributed with the package.
