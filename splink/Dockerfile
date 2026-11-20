# Built from source on the device by the Supervisor (no prebuilt image).
#
# Stage 1 fetches the repository at SPLINK_REF (set in build.yaml) and publishes it
# framework-dependent: the output is portable IL, so the same steps work on aarch64
# and amd64 and need no runtime identifier. Stage 2 is the matching .NET runtime on
# Alpine.
#
# Deliberately fixed FROM lines rather than ${BUILD_FROM}: the Supervisor may pass
# its own s6-overlay base, which has no .NET runtime.

FROM mcr.microsoft.com/dotnet/sdk:10.0-alpine AS build
ARG SPLINK_REPO=https://github.com/peter-dolkens/splink-cli.git
ARG SPLINK_REF
ARG BUILD_VERSION=0.0.0-dev
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
RUN test -n "$SPLINK_REF" \
 && git init -q /src \
 && git -C /src fetch -q --depth 1 "$SPLINK_REPO" "$SPLINK_REF" \
 && git -C /src checkout -q FETCH_HEAD \
 && git -C /src log -1 --format='splink source %H (%cs)'
RUN dotnet publish /src/src/SpLink.Cli -c Release --no-self-contained -o /out \
      -p:Version="$BUILD_VERSION" -p:SourceRevisionId="$SPLINK_REF" --nologo \
 && rm -f /out/*.pdb \
 # Only Linux native assets are needed (System.IO.Ports' serial shim).
 && find /out/runtimes -mindepth 1 -maxdepth 1 ! -name 'linux*' -exec rm -rf {} +

FROM mcr.microsoft.com/dotnet/runtime:10.0-alpine
RUN apk add --no-cache jq tzdata
COPY --from=build /out /opt/splink
COPY run.sh /run.sh
RUN chmod a+x /run.sh /opt/splink/splink \
 && /opt/splink/splink --version
ENTRYPOINT []
CMD [ "/run.sh" ]
