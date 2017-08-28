#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
from types import SimpleNamespace
from typing import Awaitable, List, Optional, TYPE_CHECKING

from pyee import EventEmitter

from pyppeteer import helper
from pyppeteer.connection import Session
from pyppeteer.input import Mouse
from pyppeteer.element_handle import ElementHandle

if TYPE_CHECKING:
    from typing import Dict, Set  # noqa


class FrameManager(EventEmitter):
    Events = SimpleNamespace(
        FrameAttached='frameattached',
        FrameNavigated='framenavigated',
        FrameDetached='framedetached'
    )

    def __init__(self, client: Session, mouse: Mouse = None) -> None:
        super().__init__()
        self._client = client
        self._mouse = mouse
        self._frames: Dict[str, Frame] = dict()
        self._mainFrame: Optional[Frame] = None

        client.on('Page.frameAttached',
                  lambda event: self._onFrameAttached(
                      event.get('frameId'), event.get('parentFrameId')))
        client.on('Page.frameNavigated',
                  lambda event: self._onFrameNavigated(event.get('frame')))
        client.on('Page.frameDetached',
                  lambda event: self._onFrameDetached(event.get('frameId')))
        client.on('Runtime.executionContextCreated',
                  lambda event: self._onExecutionContextCreated(
                      event.get('context')))

    @property
    def mainFrame(self) -> Optional['Frame']:
        return self._mainFrame

    def frames(self) -> List['Frame']:
        return list(self._frames.values())

    def _onFrameAttached(self, frameId: str, parentFrameId: Optional[str]
                         ) -> None:
        if frameId in self._frames:
            return
        parentFrame = self._frames.get(parentFrameId)
        frame = Frame(self._client, self._mouse, parentFrame, frameId)
        self._frames[frameId] = frame
        self.emit(FrameManager.Events.FrameAttached, frame)

    def _onFrameNavigated(self, framePayload: dict) -> None:
        isMainFrame = not framePayload.get('parentId')
        if isMainFrame:
            frame = self._mainFrame
        else:
            self._frames.get(framePayload.get('id'))
        if not (isMainFrame or frame):
            Exception('We either navigate top level or have old version of the navigated frame')  # noqa: #501

        # Detach all child frames first.
        if frame:
            for child in frame.childFrames:
                self._removeFramesRecursively(child)

        # Update or create main frame.
        _id = framePayload.get('id')
        if isMainFrame:
            if frame:
                # Update frame id to retain frame identity on cross-process navigation.  # noqa: E501
                self._frames.pop(frame._id, None)
                frame._id = _id
            else:
                # Initial main frame navigation.
                frame = Frame(self._client, self._mouse, None, _id)
            self._frames[_id] = frame
            self._mainFrame = frame

        # Update frame payload.
        frame._navigated(framePayload)
        self.emit(FrameManager.Events.FrameNavigated, frame)

    def _onFrameDetached(self, frameId: str) -> None:
        frame = self._frames.get(frameId)
        if frame:
            self._removeFramesRecursively(frame)

    def _onExecutionContextCreated(self, context: dict) -> None:
        auxData = context.get('auxData')
        frameId = (auxData.get('frameId')
                   if auxData and auxData.get('isDefault')
                   else None)
        frame = self._frames.get(frameId)
        if not frame:
            return
        frame._defaultContextId = context.get('id')
        for waitTask in frame._waitTasks:
            waitTask.rerun()

    def _removeFramesRecursively(self, frame: 'Frame') -> None:
        for child in frame.childFrames:
            self._removeFramesRecursively(child)
        frame._detach()
        self._frames.pop(frame._id, None)
        self.emit(FrameManager.Events.FrameDetached, frame)

    def isMainFrameLoadingFailed(self) -> bool:
        mainFrame = self._mainFrame
        if not mainFrame:
            return True
        return bool(mainFrame._loadingFailed)


class Frame(object):
    def __init__(self, client: Session, mouse: Mouse, parentFrame: 'Frame',
                 frameId: str) -> None:
        self._client = client
        self._mouse = mouse
        self._parentFrame = parentFrame
        self._url = ''
        self._detached = False
        self._id = frameId
        self._defaultContextId = '<not-initialized>'
        self._waitTasks: Set[WaitTask] = set()  # maybe list
        self._childFrames: Set[Frame] = set()  # maybe list
        if self._parentFrame:
            self._parentFrame._childFrames.add(self)

    async def evaluate(self, pageFunction: str, *args: str) -> dict:
        remoteObject = await self._rawEvaluate(pageFunction, *args)
        return await helper.serializeRemoteObject(self._client, remoteObject)

    async def J(self, selector: str) -> Optional['ElementHandle']:
        remoteObject = await self._rawEvaluate(
            'selector => document.querySelector(selector)', selector)
        if remoteObject.get('subtype') == 'node':
            return ElementHandle(self._client, remoteObject, self._mouse)
        await helper.releaseObject(self._client, remoteObject)
        return None

    async def _rawEvaluate(self, pageFunction: str, *args: str) -> dict:
        expression = helper.evaluationString(pageFunction, *args)
        contextId = self._defaultContextId
        obj = await (await self._client.send('Runtime.evaluate', {
            'expression': expression,
            'contextId': contextId,
            'returnByValue': False,
            'awaitPromise': True,
        }))
        exceptionDetails = obj.get('exceptionDetails')
        remoteObject = obj.get('result')
        if exceptionDetails:
            raise Exception('Evaluation failed: ' + helper.getExceptionMessage(exceptionDetails))  # noqa: E501
        return remoteObject

    @property
    def name(self) -> str:
        return self.__dict__.get('_name', '')

    @property
    def url(self) -> str:
        return self._url

    @property
    def parentFrame(self) -> 'Frame':
        return self._parentFrame

    @property
    def childFrames(self) -> List['Frame']:
        return list(self._childFrames)

    @property
    def idDetached(self) -> bool:
        return self._detached

    async def injectFile(self, filePath: str) -> str:
        # to be changed to async func
        with open(filePath) as f:
            contents = f.read()
        contents += f'//# sourceURL=' + filePath.replace('\n', '')
        return await self.evaluate(contents)

    async def addScriptTag(self, url: str) -> dict:
        addScriptTag = '''
function addScriptTag(url) {
  let script = document.createElement('script');
  script.src = url;
  let promise = new Promise(x => script.onload = x);
  document.head.appendChild(script);
  return promise;
}
        '''
        return await self.evaluate(addScriptTag, url)

    def waitFor(self, selectorOrFunctionOrTimeout, options: dict = None
                ) -> Awaitable:
        if options is None:
            options = dict()
        if isinstance(selectorOrFunctionOrTimeout, str):
            return self.waitForSelector(selectorOrFunctionOrTimeout, options)
        if isinstance(selectorOrFunctionOrTimeout, (int, float)):
            fut: Awaitable[None] = asyncio.ensure_future(
                asyncio.sleep(selectorOrFunctionOrTimeout))
            return fut
        if callable(selectorOrFunctionOrTimeout):
            return self.waitForFunction(selectorOrFunctionOrTimeout, options)
        fut = asyncio.get_event_loop().create_future()
        fut.set_exception(Exception(
            'Unsupported target type: ' +
            str(type(selectorOrFunctionOrTimeout))
        ))
        return fut

    def waitForSelector(self, selector: str, options: dict = None
                        ) -> Awaitable:
        if options is None:
            options = dict()
        timeout = options.get('timeout', 30000)
        waitForVisible = bool(options.get('visible'))
        polling = 'raf' if waitForVisible else 'mutation'
        predicate = '''
function predicate(selector, waitForVisible) {
  const node = document.querySelector(selector);
  if (!node)
    return false;
  if (!waitForVisible)
    return true;
  const style = window.getComputedStyle(node);
  return style && style.display !== 'none' && style.visibility !== 'hidden';
}
'''  # noqa: E501
        return self.waitForFunction(
            predicate,
            {'timeout': timeout, 'polling': polling},
            selector,
            waitForVisible
        )

    def waitForFunction(self, pageFunction: str, options: dict = None,
                        *args: str) -> 'WaitTask':
        if options is None:
            options = dict()
        timeout = options.get('timeout',  30000)
        polling = options.get('polling', 'raf')
        predicateCode = 'return ' + helper.evaluationString(pageFunction,
                                                            *args)
        return WaitTask(self, predicateCode, polling, timeout).promise

    async def title(self) -> str:
        return await self.evaluate('() => document.title')

    def _navigated(self, framePayload: dict) -> None:
        self._name = framePayload.get('name')
        self._url = framePayload.get('url')
        self._loadingFailed = bool(framePayload.get('unreachableUrl'))

    def _detach(self) -> None:
        for waitTask in self._waitTasks:
            waitTask.terminate(
                Exception('waitForSelector failed: frame got detached.'))
        self._detached = True
        if self._parentFrame:
            self._parentFrame._childFrames.remove(self)
        self._parentFrame = None


class WaitTask(object):
    def __init__(self, frame: Frame, predicateBody: str, polling: str,
                 timeout: float) -> None:
        if isinstance(polling, str) and polling not in ('raf', 'mutation'):
            raise Exception('Unknown polling option: ' + polling)
        elif isinstance(polling, (int, float)):
            if polling <= 0:
                raise Exception(
                    f'Cannot poll with non-positive interval: {polling}')
        else:
            raise Exception('Unknown polling options: ' + str(polling))

        self._frame = frame
        self._pageScript = helper.evaluationString(
            waitForPredicatePageFunction, predicateBody, polling, timeout)
        self._runCount = 0
        frame._waitTasks.add(self)
        self._promise = asyncio.get_event_loop().create_future()
        # this.promise = new Promise((resolve, reject) => {
        #     this._resolve = resolve
        #     this._reject = reject
        # })
        # Since page navigation requires us to re-install the pageScript,
        # we should track timeout on our end.
        # this._timeoutTimer = setTimeout(
        # () => this.terminate(
        # new Error(`waiting failed: timeout ${timeout}ms exceeded`)), timeout)
        self.rerun()

    def terminate(self, error: Exception) -> None:
        self._terminated = True
        self._promise.set_exception(error)
        self._cleanup()

    async def rerun(self) -> None:
        self._runCount += 1
        runCount = self._runCount
        success = False
        error = None
        try:
            success = await self._frame.evaluate(self._pageScript)
        except Exception as e:
            error = e

        if self._terminated or runCount != self._runCount:
            return

        # // Ignore timeouts in pageScript - we track timeouts ourselves.
        if not success and not error:
            return

        # // When the page is navigated, the promise is rejected.
        # // We will try again in the new execution context.
        if error and error.message.includes('Execution context was destroyed'):
            return

        if error:
            self._promise.set_exception(error)
        else:
            self._promise.set_result(None)
        self._cleanup()

    def _cleanup(self) -> None:
        # clearTimeout(this._timeoutTimer)
        self._frame._waitTasks.remove(self)
        self._runningTask = None


waitForPredicatePageFunction = '''
async function waitForPredicatePageFunction(predicateBody, polling, timeout) {
  const predicate = new Function(predicateBody);
  let timedOut = false;
  setTimeout(() => timedOut = true, timeout);
  if (polling === 'raf')
    await pollRaf();
  else if (polling === 'mutation')
    await pollMutation();
  else if (typeof polling === 'number')
    await pollInterval(polling);
  return !timedOut;

  /**
   * @return {!Promise<!Element>}
   */
  function pollMutation() {
    if (predicate())
      return Promise.resolve();

    let fulfill;
    const result = new Promise(x => fulfill = x);
    const observer = new MutationObserver(mutations => {
      if (timedOut || predicate()) {
        observer.disconnect();
        fulfill();
      }
    });
    observer.observe(document, {
      childList: true,
      subtree: true
    });
    return result;
  }

  /**
   * @return {!Promise}
   */
  function pollRaf() {
    let fulfill;
    const result = new Promise(x => fulfill = x);
    onRaf();
    return result;

    function onRaf() {
      if (timedOut || predicate())
        fulfill();
      else
        requestAnimationFrame(onRaf);
    }
  }

  /**
   * @param {number} pollInterval
   * @return {!Promise}
   */
  function pollInterval(pollInterval) {
    let fulfill;
    const result = new Promise(x => fulfill = x);
    onTimeout();
    return result;

    function onTimeout() {
      if (timedOut || predicate())
        fulfill();
      else
        setTimeout(onTimeout, pollInterval);
    }
  }
}
'''