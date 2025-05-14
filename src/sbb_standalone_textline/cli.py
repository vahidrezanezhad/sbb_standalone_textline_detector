import sys
import click
from ocrd_utils import initLogging, setOverrideLogLevel
from sbb_standalone_textline.sbb_standalone_textline import Sbb_standalone_textline


@click.command()
@click.option(
    "--image",
    "-i",
    help="image filename",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
)
@click.option(
    "--out",
    "-o",
    help="directory to write output xml data",
    type=click.Path(exists=True, file_okay=False),
    required=True,
)
@click.option(
    "--model",
    "-m",
    help="directory of models",
    type=click.Path(exists=True, file_okay=False),
)
@click.option(
    "--log-level",
    "-l",
    type=click.Choice(['OFF', 'DEBUG', 'INFO', 'WARN', 'ERROR']),
    help="Override log level globally to this",
)
def main(
    image,
    out,
    model,
    log_level
):
    if log_level:
        setOverrideLogLevel(log_level)
    initLogging()

    sbb_standalone_textline = Sbb_standalone_textline(
        image_filename=image,
        dir_out=out,
        dir_models=model,
    )
    pcgts = sbb_standalone_textline.run()
    #eynollah.writer.write_pagexml(pcgts)

if __name__ == "__main__":
    main()
