"""Small shared errors for read-only committed Recipe projections."""


class PipelineRecipeNotFoundError(Exception):
    """The requested durable pipeline run does not exist for Recipe inspection."""


class PipelineRecipeProjectionError(Exception):
    """A committed Recipe cannot be safely projected for a read-only client."""
