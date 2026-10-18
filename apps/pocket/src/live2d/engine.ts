/// <reference path="../../vendor/cubism/Core/live2dcubismcore.d.ts" />

import { CubismDefaultParameterId } from '@framework/cubismdefaultparameterid'
import { CubismModelSettingJson } from '@framework/cubismmodelsettingjson'
import { CubismEyeBlink } from '@framework/effect/cubismeyeblink'
import { CubismIdHandle } from '@framework/id/cubismid'
import { CubismFramework, LogLevel, Option } from '@framework/live2dcubismframework'
import { CubismMatrix44 } from '@framework/math/cubismmatrix44'
import { CubismUserModel } from '@framework/model/cubismusermodel'
import { CubismMotion } from '@framework/motion/cubismmotion'
import { CubismRenderer } from '@framework/rendering/cubismrenderer'
import { csmVector } from '@framework/type/csmvector'
import { figurePose, type BodyMotion, type FigureInput, type LookPoint } from './pose'

const priorityIdle = 1
const prioritySpeak = 2
const priorityNod = 3

export interface FigureEngine {
  nod(): void
  dispose(): void
}

let mountQueue: Promise<void> = Promise.resolve()

export function mountFigure(
  canvas: HTMLCanvasElement,
  modelUrl: string,
  read: () => { input: FigureInput; look: LookPoint },
  alive: () => boolean
): Promise<FigureEngine> {
  const job = mountQueue.then(() => openFigure(canvas, modelUrl, read, alive))

  mountQueue = job.then(() => undefined, () => undefined)

  return job
}

async function openFigure(
  canvas: HTMLCanvasElement,
  modelUrl: string,
  read: () => { input: FigureInput; look: LookPoint },
  alive: () => boolean
): Promise<FigureEngine> {
  if (!alive()) return idleEngine()
  await loadCore()
  if (!alive()) return idleEngine()
  const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: true })

  if (!gl) throw new Error('WebGL 不可用')
  CubismRenderer.staticRelease()
  const figure = new AdultFigure()

  try {
    await figure.load(gl, modelUrl)
  } catch (error) {
    figure.dispose(gl)
    throw error
  }

  if (!alive()) {
    figure.dispose(gl)

    return idleEngine()
  }

  let frame = 0
  let nod = false
  let disposed = false
  const loop = (now: number) => {
    if (disposed) return
    frame = requestAnimationFrame(loop)
    const sample = read()

    figure.draw(canvas, sample.input, sample.look, nod, now)
    nod = false
  }

  frame = requestAnimationFrame(loop)

  return {
    nod: () => {
      nod = true
    },
    dispose: () => {
      if (disposed) return
      disposed = true
      cancelAnimationFrame(frame)
      figure.dispose(gl)
    }
  }
}

function idleEngine(): FigureEngine {
  return { nod: () => undefined, dispose: () => undefined }
}

class AdultFigure extends CubismUserModel {
  private idle: CubismMotion | null = null
  private speak: CubismMotion | null = null
  private nodMotion: CubismMotion | null = null
  private lipSyncIds = new csmVector<CubismIdHandle>()
  private angleX: CubismIdHandle | null = null
  private angleY: CubismIdHandle | null = null
  private angleZ: CubismIdHandle | null = null
  private eyeX: CubismIdHandle | null = null
  private eyeY: CubismIdHandle | null = null
  private browL: CubismIdHandle | null = null
  private browR: CubismIdHandle | null = null
  private bodyX: CubismIdHandle | null = null
  private lean: CubismIdHandle | null = null
  private mouth: CubismIdHandle | null = null
  private clip: 'idle' | 'speak' | 'nod' | 'none' = 'none'
  private shown: LookPoint = { x: 0, y: 0 }
  private last = 0
  private projection = new CubismMatrix44()
  private textures: WebGLTexture[] = []

  async load(gl: WebGLRenderingContext, modelUrl: string): Promise<void> {
    const option = new Option()

    option.logFunction = () => undefined
    option.loggingLevel = LogLevel.LogLevel_Warning
    CubismFramework.startUp(option)
    CubismFramework.initialize()
    const dir = modelUrl.slice(0, modelUrl.lastIndexOf('/') + 1)
    const settingBuffer = await fetchBuffer(modelUrl)
    const setting = new CubismModelSettingJson(settingBuffer, settingBuffer.byteLength)
    const moc = await fetchBuffer(dir + setting.getModelFileName())

    this.loadModel(moc, true)
    if (!this.getModel()) throw new Error('模型没有载入')
    const matrix = this.getModelMatrix()

    matrix.setHeight(2.15)
    // The canvas origin is its center, so translation stays at 0.
    // setCenterPosition assumes a corner origin and shifts the figure left and down.
    const physics = setting.getPhysicsFileName()

    if (physics) {
      const buffer = await fetchBuffer(dir + physics)

      this.loadPhysics(buffer, buffer.byteLength)
    }

    this._eyeBlink = CubismEyeBlink.create(setting)
    const lips = setting.getLipSyncParameterCount()

    for (let index = 0; index < lips; index += 1) this.lipSyncIds.pushBack(setting.getLipSyncParameterId(index))
    this.idle = await this.motionIn(setting, dir, 'Idle', true)
    this.nodMotion = await this.motionIn(setting, dir, 'Nod', false)
    this.speak = await this.motionFrom(`${dir}speak.motion3.json`, true)
    const ids = CubismFramework.getIdManager()

    this.angleX = ids.getId(CubismDefaultParameterId.ParamAngleX)
    this.angleY = ids.getId(CubismDefaultParameterId.ParamAngleY)
    this.angleZ = ids.getId(CubismDefaultParameterId.ParamAngleZ)
    this.eyeX = ids.getId(CubismDefaultParameterId.ParamEyeBallX)
    this.eyeY = ids.getId(CubismDefaultParameterId.ParamEyeBallY)
    this.browL = ids.getId(CubismDefaultParameterId.ParamBrowLY)
    this.browR = ids.getId(CubismDefaultParameterId.ParamBrowRY)
    this.bodyX = ids.getId(CubismDefaultParameterId.ParamBodyAngleX)
    // ParamBodyLean is on this model and is not in the SDK's default id table.
    this.lean = ids.getId('ParamBodyLean')
    this.mouth = ids.getId(CubismDefaultParameterId.ParamMouthOpenY)
    this.createRenderer()
    this.getRenderer().setIsPremultipliedAlpha(true)
    this.getRenderer().startUp(gl)
    const count = setting.getTextureCount()

    for (let index = 0; index < count; index += 1) {
      const texture = await loadTexture(gl, dir + setting.getTextureFileName(index))

      this.textures.push(texture)
      this.getRenderer().bindTexture(index, texture)
    }
  }

  draw(canvas: HTMLCanvasElement, input: FigureInput, look: LookPoint, nod: boolean, now: number): void {
    const gl = this.getRenderer().gl
    const model = this.getModel()

    if (!gl || !model) return
    const delta = this.last === 0 ? 0 : Math.min(0.05, (now - this.last) / 1000)

    this.last = now
    const pose = figurePose(input)
    const ratio = Math.min(window.devicePixelRatio || 1, 2)
    const width = Math.max(1, Math.floor(canvas.clientWidth * ratio))
    const height = Math.max(1, Math.floor(canvas.clientHeight * ratio))

    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width
      canvas.height = height
    }

    this.shown.x += (look.x - this.shown.x) * Math.min(1, delta * 8)
    this.shown.y += (look.y - this.shown.y) * Math.min(1, delta * 8)
    model.loadParameters()
    this.chooseMotion(pose.motion, nod, input.reducedMotion)
    const moving = this._motionManager.updateMotion(model, delta)

    model.saveParameters()
    if (!moving && this._eyeBlink) this._eyeBlink.updateParameters(model, delta)
    const lookX = clamp(this.shown.x * 0.85 + pose.lookX)
    const lookY = clamp(this.shown.y * 0.85 + pose.lookY)

    this.add(this.angleX, lookX * 30)
    this.add(this.angleY, lookY * 30)
    this.add(this.angleZ, pose.tilt * 20 + lookX * lookY * -10)
    this.add(this.bodyX, lookX * 8)
    this.add(this.eyeX, lookX)
    this.add(this.eyeY, lookY)
    this.add(this.browL, pose.brow)
    this.add(this.browR, pose.brow)
    this.add(this.lean, pose.lean * 10)
    if (pose.mouth != null && this.mouth) model.setParameterValueById(this.mouth, pose.mouth)
    if (!input.reducedMotion && this._physics) this._physics.evaluate(model, delta)
    model.update()
    this.projection.loadIdentity()
    if (width > height) this.projection.scale(height / width, 1)
    else this.projection.scale(1, width / height)
    this.projection.multiplyByMatrix(this.getModelMatrix())
    this.getRenderer().setMvpMatrix(this.projection)
    gl.viewport(0, 0, width, height)
    gl.clearColor(0, 0, 0, 0)
    gl.clear(gl.COLOR_BUFFER_BIT)
    this.getRenderer().setRenderState(null as unknown as WebGLFramebuffer, [0, 0, width, height])
    this.getRenderer().drawModel()
  }

  dispose(gl: WebGLRenderingContext): void {
    for (const texture of this.textures) gl.deleteTexture(texture)
    this.textures = []
    this.release()
    CubismRenderer.staticRelease()
  }

  private chooseMotion(motion: BodyMotion, nod: boolean, reduced: boolean): void {
    if (nod && this.nodMotion && !reduced) {
      this._motionManager.stopAllMotions()
      this.play(this.nodMotion, priorityNod)
      this.clip = 'nod'

      return
    }

    if (this.clip === 'nod') {
      if (!this._motionManager.isFinished()) return
      this.clip = 'none'
    }

    const next = motion === 'speak' && this.speak ? 'speak' : motion === 'still' || !this.idle ? 'none' : 'idle'

    if (next === this.clip) return
    this._motionManager.stopAllMotions()
    if (next === 'speak' && this.speak) this.play(this.speak, prioritySpeak)
    else if (next === 'idle' && this.idle) this.play(this.idle, priorityIdle)
    this.clip = next
  }

  private play(motion: CubismMotion, priority: number): void {
    this._motionManager.setReservePriority(priority)
    this._motionManager.startMotionPriority(motion, false, priority)
  }

  private async motionIn(
    setting: CubismModelSettingJson,
    dir: string,
    group: string,
    loop: boolean
  ): Promise<CubismMotion | null> {
    if (setting.getMotionCount(group) < 1) return null

    return this.motionFrom(dir + setting.getMotionFileName(group, 0), loop)
  }

  private async motionFrom(url: string, loop: boolean): Promise<CubismMotion | null> {
    try {
      const response = await fetch(url)

      if (!response.ok) return null
      const buffer = await response.arrayBuffer()
      const motion = this.loadMotion(buffer, buffer.byteLength, url)

      if (!motion) return null
      motion.setIsLoop(loop)
      motion.setIsLoopFadeIn(false)
      motion.setEffectIds(this._eyeBlink ? this._eyeBlink.getParameterIds() : new csmVector<CubismIdHandle>(), this.lipSyncIds)

      return motion
    } catch {
      return null
    }
  }

  private add(id: CubismIdHandle | null, value: number): void {
    if (id) this.getModel().addParameterValueById(id, value)
  }
}

function clamp(value: number): number {
  if (value < -1) return -1
  if (value > 1) return 1

  return value
}

async function fetchBuffer(url: string): Promise<ArrayBuffer> {
  const response = await fetch(url)

  if (!response.ok) throw new Error(`缺少 ${url}`)

  return response.arrayBuffer()
}

function loadTexture(gl: WebGLRenderingContext, url: string): Promise<WebGLTexture> {
  return new Promise((resolve, reject) => {
    const image = new Image()

    image.onload = () => {
      const texture = gl.createTexture()

      if (!texture) {
        reject(new Error(url))

        return
      }

      gl.bindTexture(gl.TEXTURE_2D, texture)
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, 1)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image)
      gl.generateMipmap(gl.TEXTURE_2D)
      gl.bindTexture(gl.TEXTURE_2D, null)
      resolve(texture)
    }
    image.onerror = () => reject(new Error(url))
    image.src = url
  })
}

let core: Promise<void> | null = null

function loadCore(): Promise<void> {
  if (typeof Live2DCubismCore !== 'undefined') return Promise.resolve()
  core ??= new Promise((resolve, reject) => {
    const script = document.createElement('script')

    script.src = `${import.meta.env.BASE_URL}cubism/live2dcubismcore.min.js`
    script.onload = () => resolve()
    script.onerror = () => reject(new Error('Cubism Core 没有加载'))
    document.head.appendChild(script)
  })

  return core
}
