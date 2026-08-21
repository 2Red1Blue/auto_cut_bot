"""Signal management contract."""

from auto_cut_bot.channels._manifest import field, required
from auto_cut_bot.channels.contracts import ChannelSetupSpec
from auto_cut_bot.channels.plugin import ChannelPlugin

SETUP_SPEC = ChannelSetupSpec(
    fields={
        "phoneNumber": field(),
        "daemonHost": field(default="localhost"),
        "daemonPort": field("int", default=8080),
        "dm.allowFrom": field("list"),
        "group.allowFrom": field("list"),
    },
    required=(required("phoneNumber"),),
    official_url="https://github.com/bbernhard/signal-cli-rest-api",
)

PLUGIN = ChannelPlugin(
    name="signal",
    display_name="Signal",
    runtime=f"{__package__}.runtime:SignalChannel",
    setup=SETUP_SPEC,
    webui="webui/index.ts",
)
